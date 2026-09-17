"""Data model for a parsed .ket clinical message.

The model is deliberately generic: .ket files are XML messages whose element
names vary between sending systems (Adastra, Cleo, 111, local variants).  We
keep every element we find rather than mapping onto a fixed schema, so nothing
in a clinical record is silently dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

# NHS Data Dictionary person stated gender codes.
GENDER_CODES = {
    "0": "Not known",
    "1": "Male",
    "2": "Female",
    "9": "Not specified",
    "m": "Male",
    "f": "Female",
}

_ACRONYM_BOUNDARY = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_WORD_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Za-z])(?=[0-9])")


def humanise(tag: str) -> str:
    """Turn an XML tag into a readable label: NHSNumber -> 'NHS Number'."""
    label = tag.replace("_", " ").strip()
    label = _ACRONYM_BOUNDARY.sub(" ", label)
    label = _WORD_BOUNDARY.sub(" ", label)
    words = re.sub(r"\s+", " ", label).strip().split(" ")
    small = {"of", "to", "by", "for", "and", "at", "in", "the"}
    return " ".join(
        w.lower() if i and w.lower() in small else w for i, w in enumerate(words)
    )


@dataclass
class Field:
    """A single leaf element: its readable label, value and full XML path."""

    path: str
    value: str
    attributes: dict[str, str] = field(default_factory=dict)

    @property
    def tag(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def label(self) -> str:
        return humanise(self.tag)

    @property
    def group(self) -> str:
        """Parent path below the section, e.g. 'Address' — '' at section level."""
        parts = self.path.split("/")
        return "/".join(parts[1:-1])


@dataclass
class Section:
    """A top-level block of the message, e.g. Header, Patient, Encounter."""

    tag: str
    fields: list[Field] = field(default_factory=list)

    @property
    def title(self) -> str:
        return humanise(self.tag)

    def get(self, *tags: str) -> str | None:
        wanted = {t.lower() for t in tags}
        for f in self.fields:
            if f.tag.lower() in wanted:
                return f.value
        return None


@dataclass
class Payload:
    """The clinical narrative: inline HTML/text, or a base64 attachment."""

    content_type: str = "text/plain"
    text: str | None = None
    data: bytes | None = None
    attributes: dict[str, str] = field(default_factory=dict)
    index: int = 0

    @property
    def is_binary(self) -> bool:
        return self.data is not None

    @property
    def is_html(self) -> bool:
        return "html" in self.content_type.lower()

    @property
    def extension(self) -> str:
        ct = self.content_type.lower()
        for marker, ext in (
            ("pdf", ".pdf"),
            ("rtf", ".rtf"),
            ("word", ".docx"),
            ("tiff", ".tif"),
            ("jpeg", ".jpg"),
            ("png", ".png"),
            ("html", ".html"),
        ):
            if marker in ct:
                return ext
        return ".bin"

    @property
    def description(self) -> str:
        if self.is_binary:
            size = len(self.data or b"")
            return f"{self.content_type} attachment, {size:,} bytes"
        return self.content_type


@dataclass
class Record:
    """A whole .ket message."""

    source_path: str | None = None
    root_tag: str = ""
    sections: list[Section] = field(default_factory=list)
    payloads: list[Payload] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # -- lookups ---------------------------------------------------------
    def section(self, *tags: str) -> Section | None:
        wanted = {t.lower() for t in tags}
        for s in self.sections:
            if s.tag.lower() in wanted:
                return s
        return None

    def find(self, *tags: str) -> str | None:
        """First value whose leaf tag matches any of `tags`, anywhere."""
        wanted = {t.lower() for t in tags}
        for s in self.sections:
            for f in s.fields:
                if f.tag.lower() in wanted:
                    return f.value
        return None

    # -- derived patient details ----------------------------------------
    @property
    def nhs_number(self) -> str | None:
        return self.find("NHSNumber", "NHSNo", "NHSNumberValue", "PatientNHSNumber")

    @property
    def patient_name(self) -> str:
        surname = self.find("Surname", "FamilyName", "LastName") or ""
        forenames = self.find("Forenames", "Forename", "GivenName", "FirstName") or ""
        title = self.find("Title", "PatientTitle") or ""
        full = self.find("PatientName", "FullName")
        if not (surname or forenames) and full:
            return full
        name = f"{surname.upper()}, {forenames}".strip(", ")
        return f"{name} ({title})".strip() if title else name or "Unknown patient"

    @property
    def date_of_birth(self) -> str | None:
        return self.find("DateOfBirth", "DOB", "BirthDate")

    @property
    def gender(self) -> str | None:
        raw = self.find("Gender", "Sex", "PersonStatedGender")
        if raw is None:
            return None
        return GENDER_CODES.get(raw.strip().lower(), raw)

    @property
    def encounter_datetime(self) -> str | None:
        return self.find(
            "EncounterDateTime", "ConsultationDateTime", "ContactDateTime", "EventDateTime"
        )

    @property
    def created_datetime(self) -> str | None:
        return self.find("CreationDateTime", "MessageDateTime", "CreatedDateTime")

    @property
    def clinician(self) -> str | None:
        return self.find("ClinicianName", "AuthorName", "Clinician", "ResponsibleClinician")

    @property
    def source_system(self) -> str | None:
        return self.find("SourceSystem", "SendingSystem", "Originator")

    @property
    def message_id(self) -> str | None:
        return self.find("MessageID", "MessageId", "DocumentID")

    @property
    def age_at_encounter(self) -> str | None:
        dob = parse_datetime(self.date_of_birth)
        at = parse_datetime(self.encounter_datetime) or parse_datetime(self.created_datetime)
        if not dob or not at:
            return None
        years = at.year - dob.year - ((at.month, at.day) < (dob.month, dob.day))
        if years < 0:
            return None
        return f"{years}y"

    @property
    def nhs_number_valid(self) -> bool | None:
        return validate_nhs_number(self.nhs_number)

    @property
    def title(self) -> str:
        return f"{self.patient_name} — {format_datetime(self.encounter_datetime) or 'undated'}"


def parse_datetime(value: str | None) -> datetime | None:
    """Parse the date formats seen in NHS messaging, tolerantly."""
    if not value:
        return None
    text = value.strip().rstrip("Z")
    text = re.sub(r"\.\d+$", "", text)          # drop fractional seconds
    text = re.sub(r"([+-]\d{2}:?\d{2})$", "", text)  # drop UTC offset
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y%m%d%H%M%S",
        "%Y%m%d",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
        "%d-%m-%Y",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def format_datetime(value: str | None) -> str | None:
    """Render a timestamp the way a UK clinician reads it."""
    dt = parse_datetime(value)
    if dt is None:
        return value
    if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
        return dt.strftime("%d %b %Y")
    return dt.strftime("%d %b %Y, %H:%M")


def format_nhs_number(value: str | None) -> str | None:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) != 10:
        return value
    return f"{digits[0:3]} {digits[3:6]} {digits[6:10]}"


def validate_nhs_number(value: str | None) -> bool | None:
    """Modulus 11 check. Returns None when there is nothing to check."""
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if len(digits) != 10:
        return False
    total = sum(int(d) * (10 - i) for i, d in enumerate(digits[:9]))
    remainder = 11 - (total % 11)
    if remainder == 11:
        remainder = 0
    if remainder == 10:
        return False
    return remainder == int(digits[9])


def today() -> date:
    return date.today()
