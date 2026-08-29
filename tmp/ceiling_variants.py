"""Where should the ceiling be allowed to speak?

Applying it before the peak hurts: the model's own prior is right there, and a
pooled hourly rate only adds noise.  Three gates are compared against always-on
and against leaving the posterior alone, all on the same out-of-sample scoring.
"""
import json, statistics, sys, runpy
sys.path.insert(0, "tmp")

ns = runpy.run_path("tmp/ceiling_shadow.py", run_name="_fit")
