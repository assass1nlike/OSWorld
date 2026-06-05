from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from desktop_env.providers import create_vm_manager_and_provider


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--os_type", default="Ubuntu")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    if args.keep:
        os.environ["VOLCENGINE_KEEP_INSTANCE_ON_STOP"] = "true"

    manager, provider = create_vm_manager_and_provider("volcengine", None)
    image = manager.resolve_image(args.snapshot, args.os_type)
    print(f"resolved image: {image}", flush=True)

    provider.start_emulator(image, headless=True, os_type=args.os_type)
    print(f"endpoint: {provider.get_ip_address()}", flush=True)
    print(f"instance_id: {provider.instance_id}", flush=True)

    if not args.keep:
        provider.stop_emulator()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
