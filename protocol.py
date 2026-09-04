"""Wire format for Goodgame Empire's %xt% command protocol."""

import json


def encode_xt(zone: str, cmd: str, payload) -> str:
    if not isinstance(payload, str):
        payload = json.dumps(payload)
    return f"%xt%{zone}%{cmd}%1%{payload}%"


def decode_xt(raw: str):
    """Parse a %xt%...% frame into (cmd, result, obj).

    The JSON payload can itself contain literal "%" characters (player names,
    chat text, etc.), which a naive raw.split("%") would slice into extra
    fragments and truncate. Only the first 5 fields are fixed-format;
    everything after that belongs to the JSON and has to be rejoined before
    parsing, minus the trailing empty element left by the closing "%".
    """
    parts = raw.split("%")
    cmd = parts[2]
    result = int(parts[4])

    json_parts = parts[5:]
    if json_parts and json_parts[-1] == "":
        json_parts.pop()
    payload_str = "%".join(json_parts)

    try:
        obj = json.loads(payload_str)
    except json.JSONDecodeError:
        obj = payload_str

    return cmd, result, obj
