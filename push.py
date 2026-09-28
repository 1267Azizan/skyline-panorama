"""push.py — create the GitHub repo (if needed) and push this package.

Run it AFTER authenticating, once:

    gh auth login            # choose GitHub.com, HTTPS, login with a browser
    python push.py           # or: python push.py --private

It never prints or stores your token; `gh` holds the credential.
"""
import argparse
import subprocess
import sys

REPO_DEFAULT = "skyline-panorama"
OWNER_HINT = "1267Azizan"


def run(cmd, check=True):
    print("$", " ".join(cmd))
    return subprocess.run(cmd, check=check, text=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=REPO_DEFAULT)
    ap.add_argument("--owner", default=None, help=f"default: the logged-in user ({OWNER_HINT})")
    ap.add_argument("--private", action="store_true", help="create a private repo")
    ap.add_argument("--description", default="Automated perspective panorama pipeline "
                    "(local decimation + Daytona stitching + elevation normalisation)")
    args = ap.parse_args()

    # 1. auth check
    if subprocess.run(["gh", "auth", "status"], capture_output=True).returncode != 0:
        print("Not authenticated. Run:  gh auth login", file=sys.stderr)
        return 2

    # 2. who are we
    owner = args.owner
    if not owner:
        r = subprocess.run(["gh", "api", "user", "--jq", ".login"],
                           capture_output=True, text=True, check=True)
        owner = r.stdout.strip()

    # 3. create the repo if it does not exist yet
    view = subprocess.run(["gh", "repo", "view", f"{owner}/{args.repo}"],
                          capture_output=True)
    if view.returncode != 0:
        vis = "--private" if args.private else "--public"
        run(["gh", "repo", "create", f"{owner}/{args.repo}", vis,
             "--source", ".", "--remote", "origin", "--description", args.description])
    else:
        # ensure the remote exists
        remotes = subprocess.run(["git", "remote"], capture_output=True, text=True).stdout
        if "origin" not in remotes.split():
            run(["git", "remote", "add", "origin",
                 f"https://github.com/{owner}/{args.repo}.git"])

    # 4. push
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    run(["git", "push", "-u", "origin", branch])
    print(f"\nPushed. https://github.com/{owner}/{args.repo}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
