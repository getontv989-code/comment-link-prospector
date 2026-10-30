import argparse
import json
import logging
import os

from . import comments, config, harvest, pipeline


def main():
    ap = argparse.ArgumentParser(prog="python -m prospector",
                                 description="Find open, dofollow blog comment pages. Never posts anything.")
    ap.add_argument("--harvest", metavar="FILE", help="list every outbound link on the pages listed in FILE")
    ap.add_argument("--check", metavar="URL", nargs="+", help="only check these pages and print the result")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    if args.check:
        for u in args.check:
            r = comments.check_page(u)
            r["links"] = r["links"][:10]
            print(json.dumps(r, indent=2, default=list))
        return
    if args.harvest:
        harvest.run(args.harvest)
    else:
        pipeline.run()
    # Stuck worker threads would otherwise keep the process alive after the results are written.
    logging.shutdown()
    os._exit(0)


if __name__ == "__main__":
    main()
