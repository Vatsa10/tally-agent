"""Parse JSON responses from TallyPrime 7.0+ JSONEx interface."""


def parse_json_collection(response_data: dict, element_tag: str) -> list[dict]:
    """Parse a JSONEx collection export response into a list of dicts.

    The JSONEx response structure:
    {
      "status": "1",
      "data": {
        "collection": [
          {"metadata": {"type": "Ledger", "name": "Cash"}, "name": {"value": "Cash"}, ...},
          ...
        ]
      }
    }

    We flatten each item's typed values into simple key-value dicts.
    """
    data = response_data.get("data", {})
    collection = data.get("collection", [])
    items = []
    for raw_item in collection:
        item = {}
        meta = raw_item.get("metadata", {})
        if meta.get("name"):
            item["NAME"] = meta["name"]
        for key, val in raw_item.items():
            if key == "metadata":
                continue
            ukey = key.upper()
            if isinstance(val, dict):
                # Typed value: {"type": "String", "value": "..."}
                if "value" in val:
                    item[ukey] = val["value"]
                elif "type" in val:
                    item[ukey] = val.get("value", "")
            elif isinstance(val, list):
                # Nested list (e.g. languagename) — skip for now
                pass
            else:
                item[ukey] = val
        items.append(item)
    return items


def parse_json_import_result(response_data: dict) -> dict:
    """Parse a JSONEx import response into our standard result dict.

    The JSONEx response structure:
    {
      "status": "1",
      "data": {
        "import_result": {
          "created": 1, "altered": 0, "exceptions": 0, ...
        }
      }
    }
    """
    result = {
        "created": 0,
        "altered": 0,
        "exceptions": 0,
        "last_vch_id": "",
        "last_master_id": "",
        "errors": [],
    }

    if response_data.get("status") == "0":
        errors = response_data.get("error_list", [])
        result["errors"] = errors if errors else ["JSON request failed"]
        return result

    data = response_data.get("data", {})
    ir = data.get("import_result", {})
    result["created"] = int(ir.get("created", 0))
    result["altered"] = int(ir.get("altered", 0))
    result["exceptions"] = int(ir.get("exceptions", 0))
    result["last_vch_id"] = str(ir.get("lastvchid", ""))
    result["last_master_id"] = str(ir.get("lastmid", ir.get("lastmasterid", "")))

    if result["exceptions"] > 0 and not result["errors"]:
        result["errors"].append(f"Tally reported {result['exceptions']} exception(s)")

    return result


def parse_json_report(response_data: dict) -> dict | str:
    """Parse a JSONEx report export response.

    Returns the data dict directly — it's already structured JSON that
    an LLM can reason over. Falls back to string representation if needed.
    """
    if response_data.get("status") == "0":
        errors = response_data.get("error_list", [])
        return {"error": errors}
    return response_data.get("data", {})


def is_json_supported(response_data: dict) -> bool:
    """Check if a JSON response indicates the Tally version supports JSONEx.

    Returns False if the response contains 'Unknown Request' error.
    """
    if response_data.get("status") == "0":
        errors = response_data.get("error_list", [])
        for err in errors:
            if "Unknown Request" in str(err):
                return False
    return True
