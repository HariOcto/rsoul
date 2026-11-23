"""Explicit audiobook recording preferences with honest treatment of unknowns.

Desired book metadata is not evidence about a downloaded recording. Search labels
and explicit file tags can establish a conflict; absent evidence remains unknown.
Unknown rejection is deferred until local inspection so an unlabelled search hit
can still be downloaded and checked.
"""

from dataclasses import dataclass
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reasons: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()
    unknown: Tuple[str, ...] = ()


LANGUAGE_ALIASES = {
    "english": "en", "eng": "en", "german": "de", "deutsch": "de", "deu": "de", "ger": "de",
    "french": "fr", "français": "fr", "francais": "fr", "fra": "fr", "fre": "fr",
    "italian": "it", "italiano": "it", "ita": "it", "spanish": "es", "español": "es", "espanol": "es", "spa": "es",
    "portuguese": "pt", "português": "pt", "por": "pt", "dutch": "nl", "nederlands": "nl", "nld": "nl", "dut": "nl",
    "swedish": "sv", "swe": "sv", "norwegian": "no", "nor": "no", "danish": "da", "dan": "da",
    "finnish": "fi", "fin": "fi", "polish": "pl", "pol": "pl", "russian": "ru", "rus": "ru",
    "japanese": "ja", "jpn": "ja", "chinese": "zh", "zho": "zh", "chi": "zh", "korean": "ko", "kor": "ko",
    "arabic": "ar", "ara": "ar", "hebrew": "he", "heb": "he", "hindi": "hi", "hin": "hi",
}
_KNOWN_CODES = set(LANGUAGE_ALIASES.values())
_LABEL = re.compile(r"\[([^\]]*)\]|\(([^)]*)\)|\{([^}]*)\}")
_NARRATOR_LABEL = re.compile(r"\b(?:read|narrated|performed)\s+by\s+([^\]\[(){};]+)|\bnarrator\s*[:=]\s*([^\]\[(){};]+)", re.IGNORECASE)


def _values(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def normalize_language(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    return LANGUAGE_ALIASES.get(normalized, normalized)


def _person_key(value: str) -> str:
    return " ".join(re.findall(r"\w+", value.casefold()))


def _setting(config: Any, key: str, default: str) -> str:
    return config.get("Search Settings", key, fallback=default).strip()


def _metadata_evidence(metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    metadata = metadata or {}
    return {
        "languages": {normalize_language(language) for key in ("language", "languages") for value in _values(metadata.get(key)) for language in re.split(r"[,;/|]", value) if language.strip()},
        "narrators": {_person_key(value) for key in ("narrator", "narrators") for value in _values(metadata.get(key)) if _person_key(value)},
        "abridged": metadata.get("abridged") if isinstance(metadata.get("abridged"), bool) else None,
        "booktrack": metadata.get("booktrack") if isinstance(metadata.get("booktrack"), bool) else None,
        "dramatized": metadata.get("dramatized") if isinstance(metadata.get("dramatized"), bool) else None,
    }


def recording_evidence(texts: Iterable[str], metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Extract only explicit recording labels, avoiding language words in titles."""
    found = _metadata_evidence(metadata)
    for text in texts:
        if not isinstance(text, str):
            continue
        # "The English Patient" is not proof of language. A language-only
        # bracket label or an explicit Language: field is useful evidence.
        labels = [next((part for part in parts if part), "") for parts in _LABEL.findall(text)]
        labels += re.findall(r"\blanguage\s*[:=]\s*([\w-]+)", text, flags=re.IGNORECASE)
        for label in labels:
            cleaned = re.sub(r"^(?:language|lang)\s*[:=]\s*", "", label.strip(), flags=re.IGNORECASE)
            values = [normalize_language(part) for part in re.split(r"[,;/|]", cleaned)]
            if values and all(value in _KNOWN_CODES or value in LANGUAGE_ALIASES or re.fullmatch(r"(?:" + "|".join(sorted(_KNOWN_CODES)) + r")-[a-z]{2,4}", value) for value in values):
                found["languages"].update(values)
        for match in _NARRATOR_LABEL.finditer(text):
            value = next((part for part in match.groups() if part), "").strip()
            # A later release segment is not part of the narrator's name.
            value = re.split(r"\s+[-–]\s+", value)[0]
            if value:
                found["narrators"].add(_person_key(value))
        if re.search(r"\babridged\b", text, re.IGNORECASE):
            found["abridged"] = True
        elif re.search(r"\bunabridged\b|\bunabr\b", text, re.IGNORECASE) and found["abridged"] is not True:
            found["abridged"] = False
        if re.search(r"\bbook\s*track\b", text, re.IGNORECASE):
            found["booktrack"] = True
        ordinary = re.search(r"\bsingle[-\s]+narrator\b|\bnon[-\s]+dramati[sz]ed\b", text, re.IGNORECASE)
        positive_text = re.sub(r"\bnon[-\s]+dramati[sz]ed\b", "", text, flags=re.IGNORECASE)
        if re.search(r"\bdramati[sz]ed\b|\bfull[-\s]+cast\b|\bgraphic\s*audio\b|\baudio\s+drama\b", positive_text, re.IGNORECASE):
            found["dramatized"] = True
        elif ordinary and found["dramatized"] is not True:
            found["dramatized"] = False
    return found


def evaluate_recording(config: Any, texts: Iterable[str], metadata: Optional[Dict[str, Any]] = None, *, final: bool = False) -> PolicyDecision:
    """Apply configured restrictions to source evidence, never guessing unknowns.

    Defaults are unrestricted; a configured restriction can reject explicit
    conflicts at search time. Unknown values are recorded and only block after
    local inspection when audiobook_unknown_recording=reject.
    """
    evidence = recording_evidence(texts, metadata)
    reasons: List[str] = []
    unknown: List[str] = []
    warnings: List[str] = []
    languages = {normalize_language(value) for value in _setting(config, "audiobook_languages", "").split(",") if value.strip()}
    narrators = {_person_key(value) for value in _setting(config, "audiobook_narrators", "").split("|") if value.strip()}
    if languages:
        actual = evidence["languages"]
        if not actual:
            unknown.append("language")
        else:
            unsupported = {value for value in actual if not any(value == allowed or ("-" not in allowed and value.split("-", 1)[0] == allowed) for allowed in languages)}
            if unsupported:
                reasons.append("recording language is outside audiobook_languages: " + ", ".join(sorted(unsupported)))
    if narrators:
        actual = evidence["narrators"]
        if not actual:
            unknown.append("narrator")
        elif not actual <= narrators:
            reasons.append("recording narrator is outside audiobook_narrators: " + "; ".join(sorted(actual - narrators)))
    for field in ("abridged", "booktrack", "dramatized"):
        mode = _setting(config, "audiobook_" + field, "allow").lower()
        if mode not in ("allow", "reject"):
            raise ValueError(f"audiobook_{field} must be allow or reject")
        if mode == "reject":
            if evidence[field] is True:
                reasons.append(f"{field} recording rejected by audiobook_{field}")
            elif evidence[field] is None:
                unknown.append(field)
    unknown_mode = _setting(config, "audiobook_unknown_recording", "warn").lower()
    if unknown_mode not in ("allow", "warn", "reject"):
        raise ValueError("audiobook_unknown_recording must be allow, warn or reject")
    if unknown:
        description = "recording metadata is unknown for: " + ", ".join(unknown)
        if unknown_mode == "reject" and final:
            reasons.append(description)
        elif unknown_mode == "warn" or (unknown_mode == "reject" and not final):
            warnings.append(description + (" (deferred until local inspection)" if not final else ""))
    return PolicyDecision(not reasons, tuple(reasons), tuple(warnings), tuple(unknown))


def validate_recording_files(config: Any, paths: Sequence[str], source_texts: Iterable[str] = (), records: Optional[Sequence[Dict[str, Any]]] = None) -> PolicyDecision:
    """Apply final recording policy to all inspected files and their source names.

    Pass records from audiotags.validate_audio_files to avoid reading twice. This
    function never turns an invalid structural inspection into an allowed result.
    """
    if records is None:
        from .audiotags import inspect_audio_files
        records = inspect_audio_files(paths)
    if not records or any(not record.get("valid") for record in records):
        return PolicyDecision(False, ("recording cannot be assessed because audio validation failed",))
    source_texts = list(source_texts)
    reasons: List[str] = []
    warnings: List[str] = []
    unknown: List[str] = []
    for record in records:
        name = os.path.basename(record["path"])
        texts = source_texts + [name]
        tags = record.get("tags") or {}
        texts.extend(str(tags[key]) for key in ("album", "title") if tags.get(key))
        decision = evaluate_recording(config, texts, record.get("metadata") or {}, final=True)
        reasons.extend(f"{name}: {reason}" for reason in decision.reasons)
        warnings.extend(f"{name}: {warning}" for warning in decision.warnings)
        unknown.extend(decision.unknown)
    return PolicyDecision(not reasons, tuple(dict.fromkeys(reasons)), tuple(dict.fromkeys(warnings)), tuple(dict.fromkeys(unknown)))
