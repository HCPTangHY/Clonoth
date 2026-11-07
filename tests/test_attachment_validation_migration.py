"""Attachment validation tests using local fixtures only.

Run: PYTHONDONTWRITEBYTECODE=1 python3.11 -B -m unittest discover -s tests \
    -p 'test_attachment_validation_migration.py' -v
Temporary fixtures always stay under the repository's data/temp/ directory.
"""
from __future__ import annotations

import base64
import copy
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import attachments as attachment_module
from engine.attachments import (
    attachments_to_content_parts,
    build_multimodal_content,
    prepare_messages_for_llm,
    save_attachment,
)


class AttachmentValidationTests(unittest.TestCase):
    def setUp(self):
        temp_root = ROOT / "data" / "temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="attachment-validation-", dir=temp_root)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "workspace"
        (self.root / "data").mkdir(parents=True)
        self.outside = Path(self.temp.name) / "outside"
        self.outside.mkdir()
        buffer = io.BytesIO()
        Image.new("RGB", (8, 8), "red").save(buffer, format="PNG")
        self.png = buffer.getvalue()

    def write(self, name, data):
        path = self.root / "data" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path.relative_to(self.root).as_posix()

    def resolve(self, path):
        messages = [{"role": "user", "content": [{
            "type": "image_url", "image_url": {"url": "file://" + path},
        }]}]
        return prepare_messages_for_llm(messages, self.root)[0]["content"][0]

    def test_save_uses_real_bytes_despite_spoofed_mime(self):
        saved = save_attachment(self.root, "session", self.png, filename="opaque.bin", mime_type="text/plain")
        self.assertEqual(saved["type"], "image")
        self.assertEqual(saved["mime_type"], "image/png")
        self.assertTrue(saved["path"].endswith(".png"))
        self.assertEqual((self.root / saved["path"]).read_bytes(), self.png)
        self.assertEqual(attachments_to_content_parts([saved], self.root)[0]["type"], "image_url")

    def test_save_marks_forged_image_invalid_with_reason(self):
        saved = save_attachment(self.root, "session", b"<html>not an image</html>",
                                filename="fake.png", mime_type="image/png")
        self.assertEqual(saved["type"], "file")
        self.assertEqual(saved["mime_type"], "application/octet-stream")
        self.assertTrue(saved["invalid_image"])
        self.assertIn("HTML", saved["invalid_image_reason"])
        part = attachments_to_content_parts([saved], self.root)[0]
        self.assertEqual(part["type"], "text")
        self.assertIn(saved["path"], part["text"])
        self.assertIn("HTML", part["text"])

    def test_svg_is_inline_text_and_never_vision_input(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="2" height="2"/></svg>'
        saved = save_attachment(self.root, "session", svg, filename="drawing.svg", mime_type="image/svg+xml")
        self.assertEqual(saved["type"], "file")
        self.assertEqual(saved["mime_type"], "image/svg+xml")
        self.assertNotIn("invalid_image", saved)
        part = attachments_to_content_parts([{**saved, "type": "image"}], self.root)[0]
        self.assertEqual(part["type"], "text")
        self.assertIn(svg.decode(), part["text"])
        resolved = self.resolve(saved["path"])
        self.assertEqual(resolved["type"], "text")
        self.assertIn("SVG/XML", resolved["text"])

    def test_corrupt_image_warning_contains_path_and_reason(self):
        path = self.write("broken.png", b"\x89PNG\r\n\x1a\ntruncated")
        for part in (attachments_to_content_parts([{"path": path, "type": "image"}], self.root)[0], self.resolve(path)):
            self.assertEqual(part["type"], "text")
            self.assertIn(path, part["text"])
            self.assertIn("corrupted or undecodable", part["text"])

    def test_unknown_extension_is_detected_by_bytes(self):
        path = self.write("picture.payload", self.png)
        parts = attachments_to_content_parts([{"path": path}], self.root)
        self.assertEqual(parts, [{"type": "image_url", "image_url": {"url": "file://" + path}}])
        resolved = self.resolve(path)
        self.assertTrue(resolved["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        raw = base64.b64decode(resolved["image_url"]["url"].split(",", 1)[1])
        with Image.open(io.BytesIO(raw)) as image:
            self.assertEqual(image.size, (8, 8))
            self.assertEqual(image.format, "JPEG")

    def test_unknown_extension_claimed_as_image_is_not_silently_read_as_text(self):
        path = self.write("fake.payload", b'{"not": "an image"}')
        part = attachments_to_content_parts([{"path": path, "type": "image"}], self.root)[0]
        self.assertIn("Invalid image attachment", part["text"])
        self.assertIn("JSON", part["text"])

    def test_annotation_only_lists_emitted_image_paths(self):
        image = self.write("valid.png", self.png)
        invalid = self.write("bad.png", b"not an image")
        text = self.write("notes.txt", b"notes")
        attachments = [None, {"path": image}, {"path": invalid}, {"path": text, "type": "file"},
                       {"path": "data/../outside.txt", "type": "image"}]
        original = copy.deepcopy(attachments)
        content = build_multimodal_content("Inspect", attachments, self.root)
        self.assertEqual(content[0]["text"], f"Inspect\n[Attached images: {image}]")
        self.assertEqual(attachments, original)

    def test_text_files_are_inline_only_with_an_authorized_workspace(self):
        path = self.write("notes.txt", "local text 中文".encode())
        att = {"path": path, "type": "file", "mime_type": "text/plain"}
        part = attachments_to_content_parts([att], self.root)[0]
        self.assertIn("local text 中文", part["text"])
        with patch.object(Path, "open", side_effect=AssertionError("must not read cwd")):
            deferred = attachments_to_content_parts([att, {"path": "data/picture.png", "type": "image"}])
        self.assertNotIn("local text", deferred[0]["text"])
        self.assertEqual(deferred[1]["image_url"]["url"], "file://data/picture.png")

    def test_large_text_files_are_metadata_only_without_reading(self):
        path = self.write("large.txt", b"x" * (attachment_module._TEXT_FILE_MAX_BYTES + 1))
        with patch.object(Path, "open", side_effect=AssertionError("large text must not be read")):
            part = attachments_to_content_parts([{"path": path, "type": "file"}], self.root)[0]
        self.assertIn("100KB limit", part["text"])
        self.assertNotIn("---", part["text"])

    def test_path_traversal_and_absolute_paths_are_rejected_before_reads(self):
        (self.root / "private.txt").write_text("fixture-not-for-attachments")
        bad_paths = ["data/../private.txt", "data/../../outside/private.txt",
                     str(self.root / "private.txt"), "/data/private.txt", "data\\..\\private.txt"]
        with patch.object(Path, "open", side_effect=AssertionError("rejected paths must not be read")):
            for path in bad_paths:
                with self.subTest(path=path):
                    for kind in ("file", "image"):
                        self.assertEqual(attachments_to_content_parts([{"path": path, "type": kind}], self.root), [])
                    result = self.resolve(path)
                    self.assertEqual(result["type"], "text")
                    self.assertIn("outside allowed data/", result["text"])
                    self.assertNotIn("fixture-not-for-attachments", result["text"])

    def test_symlinks_outside_data_are_rejected_before_reads(self):
        (self.outside / "private.txt").write_text("outside-fixture")
        (self.outside / "picture.png").write_bytes(self.png)
        for name in ("private.txt", "picture.png"):
            (self.root / "data" / name).symlink_to(self.outside / name)
        with patch.object(Path, "open", side_effect=AssertionError("outside symlinks must not be read")):
            for name, kind in (("private.txt", "file"), ("picture.png", "image")):
                with self.subTest(name=name):
                    path = "data/" + name
                    part = attachments_to_content_parts([{"path": path, "type": kind}], self.root)[0]
                    self.assertIn("escapes workspace data/", part["text"])
                    self.assertNotIn("outside-fixture", part["text"])
                    self.assertIn("escapes workspace data/", self.resolve(path)["text"])

    def test_symlinked_data_directory_cannot_authorize_an_external_root(self):
        (self.outside / "picture.png").write_bytes(self.png)
        (self.root / "data").rmdir()
        (self.root / "data").symlink_to(self.outside, target_is_directory=True)
        with patch.object(Path, "open", side_effect=AssertionError("external data directory must not be read")):
            self.assertIn("escapes workspace data/", self.resolve("data/picture.png")["text"])

    def test_symlinks_remaining_inside_data_are_allowed(self):
        target = self.write("picture.png", self.png)
        (self.root / "data" / "link.png").symlink_to(self.root / target)
        self.assertEqual(self.resolve("data/link.png")["type"], "image_url")

    def test_oversized_images_are_rejected_before_both_read_paths(self):
        path = self.root / "data" / "large.png"
        with path.open("wb") as output:
            output.truncate(25 * 1024 * 1024 + 1)
        with patch.object(Path, "open", side_effect=AssertionError("oversized image must not be read")):
            parts = attachments_to_content_parts([{"path": "data/large.png", "type": "image"}], self.root)
            resolved = self.resolve("data/large.png")
        for part in (parts[0], resolved):
            self.assertEqual(part["type"], "text")
            self.assertIn("25MiB limit", part["text"])

    def test_save_skips_decoding_when_source_bytes_are_oversized(self):
        with patch.object(attachment_module, "_MAX_SOURCE_IMAGE_BYTES", 8), \
                patch.object(attachment_module, "inspect_raster_image_bytes", side_effect=AssertionError("must not decode oversized bytes")):
            saved = save_attachment(self.root, "session", self.png, filename="picture.png", mime_type="image/png")
        self.assertEqual(saved["type"], "file")
        self.assertTrue(saved["invalid_image"])
        self.assertIn("25MiB limit", saved["invalid_image_reason"])

    def test_missing_file_warning_contains_path_and_reason(self):
        part = self.resolve("data/missing.png")
        self.assertEqual(part["type"], "text")
        self.assertIn("data/missing.png", part["text"])
        self.assertIn("file does not exist", part["text"])

    def test_image_is_revalidated_if_file_changes_before_provider_request(self):
        path = self.write("picture.png", self.png)
        content = attachments_to_content_parts([{"path": path}], self.root)
        self.assertEqual(content[0]["type"], "image_url")
        (self.root / path).write_bytes(b"<html>replacement</html>")
        prepared = prepare_messages_for_llm([{"role": "user", "content": content}], self.root)
        self.assertEqual(prepared[0]["content"][0]["type"], "text")
        self.assertIn("HTML", prepared[0]["content"][0]["text"])

    def test_read_stays_bounded_if_file_grows_after_stat(self):
        path = self.root / "data" / "growing.png"
        with patch.object(attachment_module, "_MAX_SOURCE_IMAGE_BYTES", 8), \
                patch.object(Path, "stat", return_value=SimpleNamespace(st_size=1)), \
                patch.object(Path, "open", return_value=io.BytesIO(b"x" * 16)):
            with self.assertRaisesRegex(ValueError, "25MiB limit"):
                attachment_module._read_image_bytes(path)

    def test_compressed_size_limit_is_kept(self):
        path = self.write("picture.png", self.png)
        with patch.object(attachment_module, "_MAX_IMAGE_BYTES", 3), \
                patch.object(attachment_module, "_compress_image_for_llm", return_value=(b"1234", "image/jpeg")):
            part = self.resolve(path)
        self.assertEqual(part["type"], "text")
        self.assertIn("10MiB limit", part["text"])

    def test_assistant_images_metadata_and_other_blocks_are_preserved_without_mutation(self):
        path = self.write("picture.png", self.png)
        remote_image = {"type": "image_url", "image_url": {"url": "https://example.invalid/image.png"}}
        other = {"type": "input_audio", "input_audio": {"data": "opaque", "format": "wav"}}
        content = [{"type": "text", "text": "answer"}, other, remote_image,
                   {"type": "image_url", "image_url": {"url": "file://" + path, "detail": "low"}}]
        messages = [{"role": "assistant", "content": content,
                     "_meta": {"metadata": {"provider": {"value": "preserved"}}}, "_dynamic": True}]
        original = copy.deepcopy(messages)
        prepared = prepare_messages_for_llm(messages, self.root)
        self.assertEqual(messages, original)
        self.assertEqual(prepared[0]["role"], "assistant")
        self.assertEqual(prepared[0]["content"][:3], content[:3])
        self.assertEqual(prepared[0]["_meta"], messages[0]["_meta"])
        self.assertNotIn("_dynamic", prepared[0])
        self.assertTrue(prepared[0]["content"][3]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(prepared[0]["content"][3]["image_url"]["detail"], "low")

    def test_repeated_image_references_use_one_resolution(self):
        path = self.write("picture.png", self.png)
        content = [{"type": "image_url", "image_url": {"url": "file://" + path}}] * 2
        with patch.object(attachment_module, "_resolve_file_url", wraps=attachment_module._resolve_file_url) as resolver:
            prepared = prepare_messages_for_llm([{"role": "user", "content": content}], self.root)
        resolver.assert_called_once()
        self.assertEqual(len(prepared[0]["content"]), 2)

    def test_compression_uses_only_allowed_decoders_and_keeps_nonzero_dimensions(self):
        source = io.BytesIO()
        Image.new("RGB", (1, 2048), "red").save(source, format="PNG")
        with patch("PIL.Image.open", wraps=Image.open) as opener:
            compressed, mime = attachment_module._compress_image_for_llm(source.getvalue(), "image/png")
        self.assertEqual(opener.call_args.kwargs["formats"], list(attachment_module._ALLOWED_PIL_DECODERS))
        self.assertEqual(mime, "image/jpeg")
        with Image.open(io.BytesIO(compressed)) as image:
            self.assertEqual(image.size, (1, 1024))

    def test_save_rejects_session_path_traversal(self):
        with self.assertRaisesRegex(ValueError, "outside allowed data/"):
            save_attachment(self.root, "../../outside", self.png, filename="picture.png")


if __name__ == "__main__":
    unittest.main()
