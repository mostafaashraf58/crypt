"""Download and smoke-test the official checkpoint. Safe to rerun after interruption."""
import argparse
import json
from pathlib import Path

from .forecast import (
    ChronosForecaster, DEFAULT_MODEL_DIR, MODEL_ID, MODEL_REVISION,
    MODEL_VERSION, file_hash, verify_download,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.verify:
        verify_download(args.model_dir)
    else:
        from huggingface_hub import snapshot_download

        print(f"Downloading {MODEL_ID} at {MODEL_REVISION}", flush=True)
        snapshot_download(
            repo_id=MODEL_ID, revision=MODEL_REVISION, local_dir=args.model_dir,
            allow_patterns=["config.json", "model.safetensors", "README.md"],
        )
    model = ChronosForecaster(args.model_dir)
    result = model.predict([100 + i * .05 for i in range(200)])
    if not args.verify:
        manifest = {
            "model_id": MODEL_ID, "revision": MODEL_REVISION,
            "model_version": MODEL_VERSION,
            "sha256": {name: file_hash(args.model_dir / name)
                       for name in ("config.json", "model.safetensors")},
        }
        pending = args.model_dir / "ready.tmp"
        pending.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        pending.replace(args.model_dir / "ready.json")
    print(f"Model ready. Test forecast {result['predicted_close']:.4f}", flush=True)


if __name__ == "__main__":
    main()
