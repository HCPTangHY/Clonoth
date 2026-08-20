"""Declarative registration faces for the engine.

Why: prompt sections, plugin HTTP routes, and future declarative registries
are extension surfaces, not inference execution code. How: collect them in
this subpackage so `engine/inference/` stays focused on the loop. Purpose:
the Contributions container mounts faces from here; new declarative surfaces
get one file each.

routes.py is supervisor-process-only: the FastAPI app lives there, so the
face is mounted on the supervisor-side EngineContext and never on the
engine-side one (`ctx.contributions.get("routes")` returns None in the
engine process).
"""
