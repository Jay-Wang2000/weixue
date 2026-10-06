"""Container entrypoint: applies weixue_patches, then runs upstream app.py as __main__."""
import runpy

import weixue_patches

weixue_patches.apply()
runpy.run_path("app.py", run_name="__main__")
