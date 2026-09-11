from lxml import etree

# Report name aliases — callers may use either form.
# "Profit and Loss A/c" is the traditional name but modern TallyPrime
# (including pre-7.0 builds like 6.2) only accepts "Profit and Loss".
REPORT_NAME_MAP = {
    "Profit and Loss A/c": "Profit and Loss",
}


def _add_static_variables(
    desc: etree._Element,
    company: str,
    username: str = "",
    password: str = "",
    from_date: str = "",
    to_date: str = "",
    extra_vars: dict[str, str] | None = None,
) -> None:
    sv = etree.SubElement(desc, "STATICVARIABLES")
    if company:
        etree.SubElement(sv, "SVCURRENTCOMPANY").text = company
    etree.SubElement(sv, "SVEXPORTFORMAT").text = "$$SysName:XML"
    if username:
        etree.SubElement(sv, "SVUSERNAME").text = username
    if password:
        etree.SubElement(sv, "SVPASSWORD").text = password
    if from_date:
        etree.SubElement(sv, "SVFROMDATE").text = from_date
    if to_date:
        etree.SubElement(sv, "SVTODATE").text = to_date
    if extra_vars:
        for key, value in extra_vars.items():
            etree.SubElement(sv, key).text = value


def build_export_report_request(
    report_name: str,
    company: str,
    username: str = "",
    password: str = "",
    from_date: str = "",
    to_date: str = "",
    extra_vars: dict[str, str] | None = None,
    tdl: str = "",
) -> bytes:
    mapped_name = REPORT_NAME_MAP.get(report_name, report_name)
    envelope = etree.Element("ENVELOPE")
    header = etree.SubElement(envelope, "HEADER")
    etree.SubElement(header, "VERSION").text = "1"
    etree.SubElement(header, "TALLYREQUEST").text = "Export"
    etree.SubElement(header, "TYPE").text = "Data"
    etree.SubElement(header, "ID").text = mapped_name

    body = etree.SubElement(envelope, "BODY")
    desc = etree.SubElement(body, "DESC")
    _add_static_variables(desc, company, username, password, from_date, to_date, extra_vars)

    if tdl:
        tdl_elem = etree.SubElement(desc, "TDL")
        tdl_msg = etree.SubElement(tdl_elem, "TDLMESSAGE")
        for child in etree.fromstring(f"<ROOT>{tdl}</ROOT>"):
            tdl_msg.append(child)

    return etree.tostring(envelope, xml_declaration=False)


# Keep as alias for backwards compatibility (tally_client imports it).
build_export_report_request_v7 = build_export_report_request


def build_export_collection_request(
    collection_name: str,
    company: str,
    username: str = "",
    password: str = "",
    from_date: str = "",
    to_date: str = "",
    extra_vars: dict[str, str] | None = None,
    tdl: str = "",
) -> bytes:
    envelope = etree.Element("ENVELOPE")
    header = etree.SubElement(envelope, "HEADER")
    etree.SubElement(header, "VERSION").text = "1"
    etree.SubElement(header, "TALLYREQUEST").text = "Export"
    etree.SubElement(header, "TYPE").text = "Collection"
    etree.SubElement(header, "ID").text = collection_name

    body = etree.SubElement(envelope, "BODY")
    desc = etree.SubElement(body, "DESC")
    _add_static_variables(desc, company, username, password, from_date, to_date, extra_vars)

    if tdl:
        tdl_elem = etree.SubElement(desc, "TDL")
        tdl_msg = etree.SubElement(tdl_elem, "TDLMESSAGE")
        for child in etree.fromstring(f"<ROOT>{tdl}</ROOT>"):
            tdl_msg.append(child)

    return etree.tostring(envelope, xml_declaration=False)


def build_import_request(
    data_xml: str,
    request_type: str,
    company: str,
    username: str = "",
    password: str = "",
) -> bytes:
    envelope = etree.Element("ENVELOPE")
    header = etree.SubElement(envelope, "HEADER")
    etree.SubElement(header, "TALLYREQUEST").text = "Import Data"

    body = etree.SubElement(envelope, "BODY")
    importdata = etree.SubElement(body, "IMPORTDATA")

    requestdesc = etree.SubElement(importdata, "REQUESTDESC")
    etree.SubElement(requestdesc, "REPORTNAME").text = request_type
    sv = etree.SubElement(requestdesc, "STATICVARIABLES")
    etree.SubElement(sv, "SVCURRENTCOMPANY").text = company
    if username:
        etree.SubElement(sv, "SVUSERNAME").text = username
    if password:
        etree.SubElement(sv, "SVPASSWORD").text = password

    requestdata = etree.SubElement(importdata, "REQUESTDATA")
    tallymsg = etree.SubElement(
        requestdata, "TALLYMESSAGE", nsmap={"UDF": "TallyUDF"}
    )
    for child in etree.fromstring(f"<ROOT>{data_xml}</ROOT>"):
        tallymsg.append(child)

    return etree.tostring(envelope, xml_declaration=False)
