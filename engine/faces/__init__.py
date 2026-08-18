"""Declarative registration faces for the engine.

Why: prompt sections and future declarative registries are extension
surfaces, not inference execution code. How: collect them in this subpackage
so `engine/inference/` stays focused on the loop. Purpose: the Contributions
container mounts faces from here; new declarative surfaces get one file each.
"""
