"""Workspace external hook plugins.

[2026-05-03] Why: the engine now scans this directory for user-provided hook
handlers. How: keep this package marker lightweight and place enabled plugins in
separate .py files. Purpose: make custom hook discovery explicit without adding
runtime behavior to package import.
"""
