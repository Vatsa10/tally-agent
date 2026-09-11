import re

from lxml import etree

# Tally sometimes includes invalid XML control characters (raw bytes or
# character references like &#3; &#4;). Strip them before parsing.
_INVALID_XML_CHARS = re.compile(
    rb"[\x00-\x08\x0b\x0c\x0e-\x1f]"
)


def _clean_xml(xml_bytes: bytes) -> bytes:
    def _strip_bad_charref(m: re.Match) -> bytes:
        val = int(m.group(1))
        if val in (0x9, 0xA, 0xD):  # valid XML whitespace chars
            return m.group(0)
        if val < 0x20:
            return b""
        return m.group(0)

    xml_bytes = re.sub(rb"&#(\d+);?", _strip_bad_charref, xml_bytes)
    xml_bytes = _INVALID_XML_CHARS.sub(b"", xml_bytes)

    # Tally sometimes emits bytes that are invalid in UTF-8 (e.g., 0xF7 followed
    # by ASCII). Re-encode via latin-1 round-trip to replace broken sequences.
    try:
        xml_bytes.decode("utf-8")
    except UnicodeDecodeError:
        xml_bytes = xml_bytes.decode("utf-8", errors="replace").encode("utf-8")

    # Tally uses UDF: namespace prefix without declaring it — inject the declaration
    # on the root element so lxml doesn't reject the document.
    if b"UDF:" in xml_bytes and b"xmlns:UDF" not in xml_bytes:
        xml_bytes = xml_bytes.replace(
            b"<ENVELOPE>", b'<ENVELOPE xmlns:UDF="TallyUDF">', 1,
        )
    return xml_bytes


def parse_collection(xml_bytes: bytes, element_tag: str) -> list[dict]:
    root = etree.fromstring(_clean_xml(xml_bytes))
    collection = root.find(".//COLLECTION")
    if collection is None:
        return []
    items = []
    for elem in collection.iter(element_tag):
        item = {}
        for attr_name, attr_value in elem.attrib.items():
            item[f"@{attr_name}"] = attr_value
        for child in elem:
            if len(child) == 0:
                item[child.tag] = child.text.strip() if child.text else ""
            else:
                sub_items = []
                for sub_child in child:
                    if len(sub_child) == 0:
                        sub_items.append(
                            {
                                sub_child.tag: sub_child.text.strip()
                                if sub_child.text
                                else ""
                            }
                        )
                item[child.tag] = (
                    sub_items
                    if sub_items
                    else etree.tostring(child, encoding="unicode")
                )
        items.append(item)
    return items


def parse_import_result(xml_bytes: bytes) -> dict:
    root = etree.fromstring(_clean_xml(xml_bytes))
    result = {
        "created": 0,
        "altered": 0,
        "exceptions": 0,
        "last_vch_id": "",
        "last_master_id": "",
        "errors": [],
    }

    # Tally returns the result either inside <IMPORTRESULT> or directly
    # under <RESPONSE> depending on the request type / Tally version.
    import_result = root.find(".//IMPORTRESULT")
    container = import_result if import_result is not None else root

    created = container.findtext("CREATED") or container.findtext(".//CREATED")
    altered = container.findtext("ALTERED") or container.findtext(".//ALTERED")
    exceptions = container.findtext("EXCEPTIONS") or container.findtext(".//EXCEPTIONS")
    result["created"] = int(created) if created else 0
    result["altered"] = int(altered) if altered else 0
    result["exceptions"] = int(exceptions) if exceptions else 0
    result["last_vch_id"] = (
        container.findtext("LASTVCHID")
        or container.findtext(".//LASTVCHID")
        or ""
    )
    result["last_master_id"] = (
        container.findtext("LASTMASTERID")
        or container.findtext("LASTMID")
        or container.findtext(".//LASTMASTERID")
        or container.findtext(".//LASTMID")
        or ""
    )

    for err in root.iter("LINEERROR"):
        if err.text:
            result["errors"].append(err.text.strip())

    # Surface exceptions as errors when nothing was created/altered
    if result["exceptions"] > 0 and not result["errors"]:
        lasterror = container.findtext("LASTERROR") or container.findtext(".//LASTERROR")
        if lasterror:
            result["errors"].append(lasterror.strip())
        else:
            result["errors"].append(f"Tally reported {result['exceptions']} exception(s)")

    return result


def parse_response_status(xml_bytes: bytes) -> bool:
    root = etree.fromstring(_clean_xml(xml_bytes))
    status = root.findtext(".//STATUS")
    if status is not None:
        return status.strip() == "1"
    errors = list(root.iter("LINEERROR"))
    return len(errors) == 0


def parse_report_raw(xml_bytes: bytes) -> str:
    root = etree.fromstring(_clean_xml(xml_bytes))
    body = root.find("BODY")
    if body is not None:
        return etree.tostring(body, encoding="unicode", pretty_print=True)
    return etree.tostring(root, encoding="unicode", pretty_print=True)
