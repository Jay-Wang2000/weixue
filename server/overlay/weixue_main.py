"""Container entrypoint: applies weixue_patches and weixue_clock, then runs upstream app.py as __main__."""
import runpy

import weixue_clock
import weixue_patches

weixue_patches.apply()
weixue_clock.apply()
runpy.run_path("app.py", run_name="__main__")
