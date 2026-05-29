import sys, json, datetime, pathlib

HERE = pathlib.Path(__file__).resolve().parent
LOG = HERE.parent / "activity.jsonl"

def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    tool = data.get("tool_name", "")
    ti = data.get("tool_input", {}) or {}
    # Pull the most useful field per tool type
    detail = (
        ti.get("command")
        or ti.get("file_path")
        or ti.get("pattern")
        or ti.get("prompt")
        or ""
    )
    if isinstance(detail, str) and len(detail) > 500:
        detail = detail[:500] + "...(truncated)"
    entry = {
        "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "tool": tool,
        "detail": detail,
    }
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass

if __name__ == "__main__":
    main()
