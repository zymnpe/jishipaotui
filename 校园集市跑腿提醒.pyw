import os
import runpy


app_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paotui_monitor_app.py")
runpy.run_path(app_path, run_name="__main__")
