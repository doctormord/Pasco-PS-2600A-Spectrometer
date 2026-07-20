"""Write the current UTC datetime into the VERSION file. Run this once when you
make a release/upload if you're not using the GitHub Action. Copy-immune and
read offline by version.py."""
import datetime, os
stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")
open(path, "w", encoding="utf-8").write(stamp + "\n")
print("VERSION =", stamp)
