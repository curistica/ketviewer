# ketviewer

> **Proof of concept.** Built to explore what a `.ket` reader could look like —
> not a product, not a medical device, and not for clinical use. Nothing here
> has been through clinical safety assurance under DCB0129/DCB0160. Do not
> point it at a live inbox of real patient records.

A local viewer for `.ket` clinical messages — the XML reports sent to a GP
practice by out-of-hours, NHS 111, emergency department and community systems.

It parses the message, shows the clinical narrative the way a clinician reads
it, and lists the demographics and routing metadata underneath. Nothing leaves
the machine: no dependencies, no network calls, no telemetry.

```bash
python3 -m ketviewer view samples/sample_ooh.ket      # opens in your browser
python3 -m ketviewer view samples/sample_ooh.ket --text
python3 -m ketviewer serve samples                    # review a folder, or upload a file
python3 -m ketviewer list  samples                    # one line per record
```

Install it as a command (`ketviewer view …`) with `pip install -e .`, or just
run it from this folder with `python3 -m ketviewer` — it needs nothing but
Python 3.10+.

## What it does

**Reads the variants.** `.ket` files are not one schema. Adastra `ReportMsg`,
ITK `ITKMessage` and local formats all use different element names, so the
parser works structurally: top-level blocks become sections, leaf elements
become fields, and **anything it doesn't recognise is still displayed** rather
than silently dropped. Field names are humanised for display (`NHSNumber` →
"NHS Number") and coded values are translated with the original kept alongside
(`Gender 1` → "Male (1)").

**Finds the clinical content.** The payload — `<Payload>`, `<Content>`,
`<Report>`, or any element with a `contentType`/`mimeType` attribute — is
pulled to the top of the page. Inline HTML, plain text and base64 attachments
are all handled.

**Shows attachments in place.** Under `serve`, a base64 PDF is rendered in the
page in the browser's own viewer, and PNG/JPEG/GIF/WebP images and text
attachments likewise; anything else gets a download button. SVG is never
previewed, because it can carry script. A standalone HTML file from `view`
offers the download instead — browsers refuse to frame a `data:` URI — and
`view --save-attachments DIR` writes them out as files.

**Takes an upload.** The `serve` index page has a file picker: choose a `.ket`
and it is parsed and displayed immediately. Uploads are held **in memory for
the life of the process only** — never written to disk, gone when you stop the
server — so a one-off file can be read without first copying it into the
folder. The limit is 25 MB.

**Checks what is checkable.** The NHS number is shown formatted with a modulus
11 check-digit verification, and age at the encounter is derived from the date
of birth — the two things most worth a second look before a record is filed to
a patient.

## Safety notes

These matter because the file comes from another organisation's system:

- **Payload markup is sanitised against an allowlist.** Scripts, event
  handlers, iframes, remote images and stylesheets are removed before
  rendering; when anything is dropped the viewer says so, and the raw XML is
  one click away. A record can't run code in the viewer, and opening one can't
  signal an external server.
- **Entity declarations are refused.** A message that declares its own XML
  entities is rejected rather than expanded ("billion laughs").
- **The server binds to 127.0.0.1 only** and serves only the folder you point
  it at, plus whatever you upload in the session. Records are addressed by an
  opaque id, never by a path from the URL, so there is nothing to traverse.
  Responses carry `no-store`, `no-referrer` and a CSP of `default-src 'none'`
  with `frame-src 'self'` and `img-src 'self' data:` — just enough to show an
  attachment from this server and nothing from anywhere else.
- **Uploads never touch disk**, and the redirect after an upload uses the
  record's opaque id, so a filename like `SMITH_John.ket` does not end up in
  browser history.
- Encoding is taken from the XML declaration, falling back to UTF-8 then
  CP1252, so `£` and accented names survive from older senders.

The sample files in `samples/` are synthetic: invented patients, fictional
places, and NHS numbers from the 999 000 0000–999 999 9999 range that NHS
England reserves for test data, so none can collide with a real patient. They
are safe to share.

Real `.ket` files are patient-identifiable. `.gitignore` excludes `*.ket`
outside `samples/` so a working inbox cannot be committed by accident — keep
them off shared folders too.

## Status and scope

Released under the MIT licence, with no warranty. It is a reading aid, not a
medical device and not a clinical system: it does not file to a patient record,
and anything it displays should be checked against the authoritative record
before a clinical decision is made. Contributions and bug reports welcome.

## Layout

| File | Purpose |
| --- | --- |
| `ketviewer/parser.py` | XML → `Record`, tolerant of schema variation |
| `ketviewer/model.py` | Record/Section/Field/Payload, NHS number and date helpers |
| `ketviewer/sanitize.py` | Allowlist HTML sanitiser for payload markup |
| `ketviewer/render.py` | Self-contained HTML and plain-text renderers |
| `ketviewer/server.py` | Loopback review server for a folder |
| `ketviewer/cli.py` | `view` / `serve` / `list` |

Run the tests with:

```bash
python3 -m unittest discover -s tests
```

## Adding a new sender's format

If a new system's messages show useful fields in the wrong place, the only
change usually needed is in `ketviewer/model.py`: add the sender's element name
to the alias list in the relevant `Record` property (for example `NHSNo`
alongside `NHSNumber`). Payload elements are recognised by name in
`PAYLOAD_TAGS` in `ketviewer/parser.py`.
