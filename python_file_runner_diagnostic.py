"""Automation Anywhere Python File Runner: run a downloaded .py and return errors.

Python: Open this runner, then Execute Function current_function(file_path),
passing the full path of the downloaded GitHub Python file.
"""

import io
import os
import runpy
import traceback
from contextlib import redirect_stdout


def current_function(file_path):
    output = io.StringIO()
    file_path = str(file_path).strip().strip('"')
    if not os.path.isfile(file_path):
        return "ERROR | File not found: " + file_path

    try:
        with redirect_stdout(output):
            runpy.run_path(file_path, run_name="__main__")
        return output.getvalue().strip() or "SUCCESS | Script ran but printed nothing"
    except BaseException as exc:
        captured = output.getvalue().strip()
        try:
            with open(file_path, "rb") as handle:
                first_line = handle.readline(160).decode("utf-8", "replace").strip()
        except Exception:
            first_line = "[could not read]"
        details = (
            "ERROR | " + type(exc).__name__ + ": " + str(exc)
            + "\nFILE: " + file_path
            + "\nFIRST LINE OF bot.py: " + repr(first_line[:140])
            + "\nTRACEBACK:\n" + traceback.format_exc()
        )
        if captured:
            details += "\nSCRIPT OUTPUT:\n" + captured
        return details
