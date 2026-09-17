"""Tests for ketviewer. Run with: python3 -m unittest discover -s tests"""

import base64
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ketviewer.model import (  # noqa: E402
    format_datetime,
    format_nhs_number,
    humanise,
    validate_nhs_number,
)
from ketviewer.parser import KetParseError, parse_bytes, parse_file  # noqa: E402
from ketviewer.render import render_record, render_text  # noqa: E402
from ketviewer.sanitize import sanitize_html  # noqa: E402

SAMPLES = Path(__file__).resolve().parents[1] / "samples"

MINIMAL = b"""<?xml version="1.0"?>
<ReportMsg>
  <Patient><NHSNumber>9990000018</NHSNumber><Surname>TEST</Surname>
    <Forenames>Ann</Forenames><DateOfBirth>1990-01-02</DateOfBirth><Gender>2</Gender></Patient>
  <Encounter><EncounterDateTime>2026-01-05T09:00:00</EncounterDateTime></Encounter>
  <Payload contentType="text/html"><![CDATA[<p>Seen and well.</p>]]></Payload>
</ReportMsg>"""


class ModelHelpers(unittest.TestCase):
    def test_humanise(self):
        self.assertEqual(humanise("NHSNumber"), "NHS Number")
        self.assertEqual(humanise("DateOfBirth"), "Date of Birth")
        self.assertEqual(humanise("Line1"), "Line 1")

    def test_nhs_number_checks(self):
        self.assertTrue(validate_nhs_number("9990000018"))
        self.assertTrue(validate_nhs_number("999 000 0018"))
        self.assertFalse(validate_nhs_number("9990000019"))
        self.assertFalse(validate_nhs_number("12345"))
        self.assertIsNone(validate_nhs_number(None))
        self.assertEqual(format_nhs_number("9990000018"), "999 000 0018")

    def test_dates(self):
        self.assertEqual(format_datetime("2026-09-15T19:15:00"), "15 Sep 2026, 19:15")
        self.assertEqual(format_datetime("1978-05-12"), "12 May 1978")
        self.assertEqual(format_datetime("15/09/2026 19:15"), "15 Sep 2026, 19:15")
        self.assertEqual(format_datetime("2026-09-15T19:15:00+01:00"), "15 Sep 2026, 19:15")
        self.assertEqual(format_datetime("not a date"), "not a date")
        self.assertIsNone(format_datetime(None))


class Parsing(unittest.TestCase):
    def test_sample_ooh(self):
        record = parse_file(SAMPLES / "sample_ooh.ket")
        self.assertEqual(record.patient_name, "SMITH, John")
        self.assertEqual(record.nhs_number, "9990000018")
        self.assertTrue(record.nhs_number_valid)
        self.assertEqual(record.gender, "Male")
        self.assertEqual(record.age_at_encounter, "48y")
        self.assertEqual(record.clinician, "Dr. Jane Doe")
        self.assertEqual([s.tag for s in record.sections], ["Header", "Patient", "Encounter"])
        self.assertEqual(len(record.payloads), 1)
        self.assertTrue(record.payloads[0].is_html)
        self.assertIn("Salbutamol", record.payloads[0].text)

    def test_namespaced_variant(self):
        record = parse_file(SAMPLES / "sample_111_triage.ket")
        self.assertEqual(record.root_tag, "ITKMessage")
        self.assertEqual(record.patient_name, "BLOGGS, Amara Ruth (Mrs)")
        self.assertEqual(record.gender, "Female")
        self.assertEqual(record.find("DispositionText"), "Speak to a primary care service within 2 hours")
        self.assertFalse(record.payloads[0].is_binary)

    def test_base64_attachment(self):
        record = parse_file(SAMPLES / "sample_discharge.ket")
        payload = record.payloads[0]
        self.assertTrue(payload.is_binary)
        self.assertEqual(payload.extension, ".pdf")
        self.assertTrue(payload.data.startswith(b"%PDF"))

    def test_address_is_grouped_not_lost(self):
        record = parse_file(SAMPLES / "sample_ooh.ket")
        patient = record.section("Patient")
        postcode = next(f for f in patient.fields if f.tag == "Postcode")
        self.assertEqual(postcode.group, "Address")
        self.assertEqual(postcode.value, "ZZ1 1AA")

    def test_element_attributes_are_kept(self):
        record = parse_bytes(
            b"<Msg><Header status='final'><MessageID>1</MessageID></Header></Msg>"
        )
        header = record.section("Header")
        self.assertEqual(header.fields[0].path, "Header/@status")
        self.assertEqual(header.fields[0].value, "final")

    def test_unknown_sections_are_preserved(self):
        record = parse_bytes(
            b"<Msg><SomethingNew><OddField>value</OddField></SomethingNew></Msg>"
        )
        self.assertEqual(record.find("OddField"), "value")

    def test_top_level_leaf_fields_are_not_dropped(self):
        record = parse_bytes(b"<Msg version='3'><Priority>Urgent</Priority></Msg>")
        self.assertEqual(record.find("Priority"), "Urgent")
        self.assertEqual(record.sections[0].fields[0].value, "3")

    def test_bad_xml_raises(self):
        with self.assertRaises(KetParseError):
            parse_bytes(b"<Msg><unclosed>")

    def test_entity_declarations_are_refused(self):
        bomb = (
            b'<?xml version="1.0"?><!DOCTYPE Msg [<!ENTITY a "aaaaaaaaaa">]>'
            b"<Msg><X>&a;</X></Msg>"
        )
        with self.assertRaises(KetParseError):
            parse_bytes(bomb)

    def test_external_dtd_reference_is_fine(self):
        doc = b'<?xml version="1.0"?><!DOCTYPE ReportMsg SYSTEM "x.dtd"><ReportMsg><A>1</A></ReportMsg>'
        self.assertEqual(parse_bytes(doc).find("A"), "1")

    def test_cp1252_content(self):
        raw = '<?xml version="1.0" encoding="windows-1252"?><Msg><A>Café £5</A></Msg>'.encode(
            "cp1252"
        )
        self.assertEqual(parse_bytes(raw).find("A"), "Café £5")

    def test_base64_payload_that_will_not_decode(self):
        doc = (
            b'<Msg><Payload contentType="application/pdf" encoding="base64">'
            b"not base64 at all!!</Payload></Msg>"
        )
        record = parse_bytes(doc)
        self.assertFalse(record.payloads[0].is_binary)
        self.assertTrue(record.warnings)


class Sanitising(unittest.TestCase):
    def test_scripts_and_handlers_removed(self):
        html, removed = sanitize_html(
            "<p onclick='steal()'>ok</p><script>alert(1)</script>"
        )
        self.assertEqual(html, "<p>ok</p>")
        self.assertIn("script", removed)

    def test_remote_resources_removed(self):
        html, removed = sanitize_html("<p>a<img src='http://tracker/x.png'></p>")
        self.assertNotIn("img", html)
        self.assertIn("img", removed)

    def test_clinical_formatting_survives(self):
        html, _ = sanitize_html(
            "<h3>Summary</h3><p><strong>BP:</strong> 130/80</p>"
            "<table><tr><th scope='col'>Drug</th><td colspan='2'>Amoxicillin</td></tr></table>"
        )
        self.assertIn("<strong>BP:</strong>", html)
        self.assertIn('<th scope="col">Drug</th>', html)
        self.assertIn('colspan="2"', html)

    def test_unbalanced_markup_is_closed(self):
        html, _ = sanitize_html("<p>one<p>two")
        self.assertTrue(html.endswith("</p>"))

    def test_text_is_escaped(self):
        html, _ = sanitize_html("5 < 6 & rising")
        self.assertIn("&lt;", html)
        self.assertIn("&amp;", html)


class Rendering(unittest.TestCase):
    def test_html_document_is_self_contained(self):
        record = parse_bytes(MINIMAL)
        html = render_record(record)
        self.assertIn("<!doctype html>", html)
        self.assertIn("Seen and well.", html)
        self.assertNotIn("http://", html.replace("http://www.w3.org", ""))

    def test_payload_script_never_reaches_output(self):
        doc = (
            b'<Msg><Payload contentType="text/html"><![CDATA['
            b"<p>note</p><script>fetch('http://x')</script>]]></Payload></Msg>"
        )
        html = render_record(parse_bytes(doc))
        self.assertNotIn("<script>", html)
        self.assertNotIn("fetch(", html)

    def test_attachment_is_embedded_as_data_uri(self):
        payload = base64.b64encode(b"%PDF-1.4 hello").decode()
        doc = f'<Msg><Payload contentType="application/pdf">{payload}</Payload></Msg>'.encode()
        html = render_record(parse_bytes(doc))
        self.assertIn("data:application/pdf;base64,", html)

    def test_image_attachment_renders_inline(self):
        png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 40).decode()
        doc = f'<Msg><Payload contentType="image/png">{png}</Payload></Msg>'.encode()
        record = parse_bytes(doc)
        from ketviewer.render import render_body

        html = render_body(record, attachment_urls={0: "/a/0"}, embed_attachments=False)
        self.assertIn("<img src='/a/0'", html)

    def test_svg_attachment_is_never_inlined(self):
        svg = base64.b64encode(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>").decode()
        doc = f'<Msg><Payload contentType="image/svg+xml" encoding="base64">{svg}</Payload></Msg>'.encode()
        from ketviewer.render import render_body

        html = render_body(parse_bytes(doc), attachment_urls={0: "/a/0"}, embed_attachments=False)
        self.assertNotIn("<img", html)
        self.assertIn("can carry script", html)

    def test_saved_html_offers_download_not_a_framed_pdf(self):
        payload = base64.b64encode(b"%PDF-1.4 hello").decode()
        doc = f'<Msg><Payload contentType="application/pdf">{payload}</Payload></Msg>'.encode()
        html = render_record(parse_bytes(doc))
        self.assertNotIn("<iframe", html)
        self.assertIn("data:application/pdf;base64,", html)

    def test_text_rendering(self):
        text = render_text(parse_bytes(MINIMAL))
        self.assertIn("TEST, Ann", text)
        self.assertIn("Seen and well.", text)
        self.assertNotIn("<p>", text)


class Serving(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import threading

        from ketviewer.server import serve

        cls.server, cls.url = serve(SAMPLES, port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _get(self, path):
        import urllib.request

        with urllib.request.urlopen(self.url.rstrip("/") + path) as response:
            return response.status, response.read(), dict(response.headers)

    def test_index_lists_samples(self):
        status, body, _ = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"SMITH, John", body)
        self.assertIn(b"BLOGGS", body)

    def test_search_filters(self):
        _, body, _ = self._get("/?q=amara")
        self.assertIn(b"BLOGGS", body)
        self.assertNotIn(b"SMITH, John", body)

    def _first_id(self, with_attachment=False):
        for entry in self.server.RequestHandlerClass.library.entries():
            if entry.record is None:
                continue
            if with_attachment and not any(p.is_binary for p in entry.record.payloads):
                continue
            return entry.id
        raise AssertionError("no suitable record")

    def test_record_page_and_headers(self):
        status, body, headers = self._get(f"/record/{self._first_id()}")
        self.assertEqual(status, 200)
        self.assertIn(b"Clinical content", body)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])

    def test_attachment_download(self):
        entry_id = self._first_id(with_attachment=True)
        status, body, headers = self._get(f"/record/{entry_id}/attachment/0")
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"%PDF"))
        self.assertEqual(headers["Content-Type"], "application/pdf")

    def test_pdf_is_framed_inline(self):
        entry_id = self._first_id(with_attachment=True)
        _, body, _ = self._get(f"/record/{entry_id}")
        self.assertIn(f"<iframe src='/record/{entry_id}/attachment/0'".encode(), body)
        self.assertIn(b"Download", body)

    def test_csp_allows_same_origin_frames_only(self):
        _, _, headers = self._get("/")
        csp = headers["Content-Security-Policy"]
        self.assertIn("frame-src 'self'", csp)
        self.assertIn("img-src 'self' data:", csp)
        self.assertIn("object-src 'none'", csp)
        self.assertIn("frame-ancestors 'self'", csp)

    def test_record_ids_are_stable_across_requests(self):
        first = self._first_id()
        self.assertEqual(first, self._first_id())
        _, body, _ = self._get(f"/record/{first}")
        self.assertEqual(200, self._get(f"/record/{first}")[0])

    def test_unknown_path_is_404(self):
        import urllib.error

        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._get("/../../etc/passwd")
        self.assertEqual(caught.exception.code, 404)
        caught.exception.close()


class Uploading(unittest.TestCase):
    """Files uploaded through the page are parsed in memory, never saved."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        import threading

        from ketviewer.server import serve

        cls.empty = Path(tempfile.mkdtemp())
        cls.server, cls.url = serve(cls.empty, port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _post(self, payload: bytes, filename="upload.ket"):
        import urllib.error
        import urllib.request

        boundary = "----ketviewerTest"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: application/xml\r\n\r\n"
        ).encode() + payload + f"\r\n--{boundary}--\r\n".encode()
        request = urllib.request.Request(
            self.url.rstrip("/") + "/upload",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, response.url, response.read()
        except urllib.error.HTTPError as exc:
            status, body = exc.code, exc.read()
            exc.close()
            return status, None, body

    def test_upload_is_parsed_and_shown(self):
        status, url, body = self._post(MINIMAL)
        self.assertEqual(status, 200)          # after the redirect to the record
        self.assertIn("/record/u", url)
        self.assertIn(b"TEST, Ann", body)
        self.assertIn(b"Seen and well.", body)

    def test_upload_is_not_written_to_disk(self):
        self._post(MINIMAL, filename="patient.ket")
        self.assertEqual(list(self.empty.iterdir()), [])

    def test_upload_appears_in_the_index(self):
        self._post(MINIMAL)
        import urllib.request

        with urllib.request.urlopen(self.url) as response:
            body = response.read()
        self.assertIn(b"uploaded", body)
        self.assertIn(b"TEST, Ann", body)

    def test_unparseable_upload_reports_the_reason(self):
        status, _, body = self._post(b"<Msg><unclosed>")
        self.assertEqual(status, 400)
        self.assertIn(b"Not valid XML", body)

    def test_upload_with_no_file_is_rejected(self):
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            self.url.rstrip("/") + "/upload",
            data=b"nothing here",
            headers={"Content-Type": "text/plain"},
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()

    def test_oversized_upload_is_refused(self):
        from ketviewer import server as server_module

        status, _, body = self._post(b"x" * (server_module.MAX_UPLOAD_BYTES + 1))
        self.assertEqual(status, 413)
        self.assertIn(b"limit", body)

    def test_uploaded_pdf_attachment_is_viewable(self):
        payload = base64.b64encode(b"%PDF-1.4 hello").decode()
        doc = (
            f'<ReportMsg><Patient><Surname>ROE</Surname></Patient>'
            f'<Payload contentType="application/pdf">{payload}</Payload></ReportMsg>'
        ).encode()
        _, url, body = self._post(doc)
        self.assertIn(b"<iframe", body)
        import urllib.request

        with urllib.request.urlopen(url.rstrip("/") + "/attachment/0") as response:
            self.assertEqual(response.headers["Content-Type"], "application/pdf")
            self.assertTrue(response.read().startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
