"""Container entrypoint: applies weixue_patches, weixue_agent_delivery, weixue_clock and weixue_device_tools, then runs upstream app.py as __main__."""
import runpy

import weixue_agent_delivery
import weixue_clock
import weixue_device_tools
import weixue_patches

weixue_patches.apply()
weixue_agent_delivery.apply()
weixue_clock.apply()
weixue_device_tools.apply()
runpy.run_path("app.py", run_name="__main__")
