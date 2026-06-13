#!/usr/bin/env python3
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Helper CLI tool for the gov-legal-assistant skill.

Handles document scanning, comparison, term extraction, and requisite
validation for Ukrainian DOCX and TXT files.
Uses only the Python standard library.
"""

import argparse
import difflib
import json
import re
import sys
import xml.etree.ElementTree as ET
import zipfile


def extract_text_from_docx(file_path):
    """Extracts plain text from a .docx file using standard zipfile and xml parsing."""
    try:
        with zipfile.ZipFile(file_path) as z:
            doc_xml = z.read('word/document.xml')
            root = ET.fromstring(doc_xml)
            
            # Namespace map for Word Processing ML
            ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
            
            paragraphs = []
            for p in root.findall('.//w:p', ns):
                text_runs = []
                for t in p.findall('.//w:t', ns):
                    if t.text:
                        text_runs.append(t.text)
                paragraphs.append("".join(text_runs))
            return "\n".join(paragraphs)
    except Exception as e:
        print(f"Error reading docx file {file_path}: {e}", file=sys.stderr)
        sys.exit(1)


def read_file_text(file_path):
    """Reads text from a TXT/Markdown or DOCX file."""
    if file_path.lower().endswith('.docx'):
        return extract_text_from_docx(file_path)
    else:
        try:
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                return f.read()
        except Exception as e:
            print(f"Error reading text file {file_path}: {e}", file=sys.stderr)
            sys.exit(1)


# ---------------------------------------------------------------------------
# Scan command
# ---------------------------------------------------------------------------

# Patterns for mandatory requisites of official orders (наказ)
REQUISITE_PATTERNS = {
    "date": re.compile(
        r'(?:'
        r'\d{2}\.\d{2}\.\d{4}'               # DD.MM.YYYY
        r'|«\s*\d{1,2}\s*»\s*\w+\s*\d{4}'    # «01» січня 2024
        r'|"\s*\d{1,2}\s*"\s*\w+\s*\d{4}'     # "01" січня 2024
        r')'
    ),
    "order_number": re.compile(
        r'(?:№\s*\d+|наказ\s+.*?№\s*\d+)', re.IGNORECASE
    ),
    "signature_block": re.compile(
        r'(?:'
        r'[Гг]олов[аи]\s+ДКА'
        r'|[Кк]ерівник'
        r'|[Пп]ідпис'
        r'|[Dd]irector'
        r'|М\.П\.'
        r'|/__+/'
        r')',
        re.IGNORECASE,
    ),
    "edrpou": re.compile(r'ЄДРПОУ|ЄДРПОУ\s*:?\s*\d{8}|код\s+ЄДРПОУ', re.IGNORECASE),
}

# Patterns for potentially outdated law references
OUTDATED_LAW_HINTS = re.compile(
    r'(?:втратив\s+чинність|скасовано|не\s+чинний)',
    re.IGNORECASE,
)

# Core scan patterns
UNDERSCORE_RE = re.compile(r'_{3,}')
BRACKET_PLACEHOLDER_RE = re.compile(r'\[[^\]]*\]')
EMPTY_PARENTHESES_RE = re.compile(r'\(\s*\)')
TODO_RE = re.compile(
    r'(todo|увага|заповнити|placeholder|проект|проєкт|зазначити|вписати'
    r'|підлягає\s+заміні|потребує\s+уточнення|необхідно\s+вказати)',
    re.IGNORECASE,
)


def _check_requisites(text):
    """Check for the presence of mandatory requisites in formal documents."""
    missing = []
    for name, pattern in REQUISITE_PATTERNS.items():
        if not pattern.search(text):
            labels = {
                "date": "Дата документу (DD.MM.YYYY або «DD» місяць YYYY)",
                "order_number": "Номер документа (№ ...)",
                "signature_block": "Блок підпису (Голова ДКА / Керівник / Підпис)",
                "edrpou": "Код ЄДРПОУ",
            }
            missing.append({
                "requisite": name,
                "description": labels.get(name, name),
                "type": "missing_requisite",
            })
    return missing


def _check_outdated_refs(lines):
    """Scan for phrases hinting at references to repealed legislation."""
    issues = []
    for idx, line in enumerate(lines, 1):
        for m in OUTDATED_LAW_HINTS.finditer(line):
            issues.append({
                "line": idx,
                "value": m.group(),
                "type": "potential_outdated_law",
                "content": line.strip(),
            })
    return issues


def cmd_scan(args):
    """Scans document for placeholders, TODOs, missing requisites, and more."""
    text = read_file_text(args.file)
    lines = text.split('\n')
    
    placeholders = []
    formatting_issues = []
    
    for idx, line in enumerate(lines, 1):
        line_stripped = line.strip()
        if not line_stripped:
            continue
            
        # Scan for underscores (___)
        for m in UNDERSCORE_RE.finditer(line):
            placeholders.append({
                "line": idx,
                "value": m.group(),
                "type": "underscores",
                "content": line_stripped
            })
            
        # Scan for square bracket placeholders ([...])
        for m in BRACKET_PLACEHOLDER_RE.finditer(line):
            placeholders.append({
                "line": idx,
                "value": m.group(),
                "type": "bracket_placeholder",
                "content": line_stripped
            })
            
        # Scan for empty parentheses ()
        for m in EMPTY_PARENTHESES_RE.finditer(line):
            placeholders.append({
                "line": idx,
                "value": m.group(),
                "type": "empty_parentheses",
                "content": line_stripped
            })
            
        # Scan for TODOs / legal review reminders
        for m in TODO_RE.finditer(line):
            placeholders.append({
                "line": idx,
                "value": m.group(),
                "type": "todo_marker",
                "content": line_stripped
            })
            
        # Check unmatched brackets in the line
        stack = []
        mapping = {')': '(', ']': '[', '}': '{'}
        for char in line:
            if char in mapping.values():
                stack.append(char)
            elif char in mapping.keys():
                if not stack or stack[-1] != mapping[char]:
                    formatting_issues.append({
                        "line": idx,
                        "type": "unmatched_delimiter",
                        "message": f"Некоректно закрита дужка '{char}'",
                        "content": line_stripped
                    })
                    break
                else:
                    stack.pop()
        if stack:
            formatting_issues.append({
                "line": idx,
                "type": "unmatched_delimiter",
                "message": f"Незакриті дужки: {', '.join(stack)}",
                "content": line_stripped
            })

    # Check mandatory requisites
    missing_requisites = _check_requisites(text)

    # Check for outdated law references
    outdated_refs = _check_outdated_refs(lines)

    output_data = {
        "summary": {
            "total_placeholders": len(placeholders),
            "total_formatting_issues": len(formatting_issues),
            "total_missing_requisites": len(missing_requisites),
            "total_outdated_law_hints": len(outdated_refs),
        },
        "placeholders": placeholders,
        "formatting_issues": formatting_issues,
        "missing_requisites": missing_requisites,
        "outdated_law_hints": outdated_refs,
    }
    
    write_json_output(output_data, args.output)


# ---------------------------------------------------------------------------
# Compare command
# ---------------------------------------------------------------------------

def cmd_compare(args):
    """Compares two documents and outputs a detailed Markdown diff."""
    text1 = read_file_text(args.file1)
    text2 = read_file_text(args.file2)
    
    lines1 = text1.splitlines()
    lines2 = text2.splitlines()
    
    # Run diff
    diff = list(difflib.unified_diff(
        lines1, lines2,
        fromfile=args.file1,
        tofile=args.file2,
        lineterm='',
    ))
    
    report = []
    report.append("# Звіт про порівняння документів")
    report.append(f"- **Перший файл (джерело)**: `{args.file1}` ({len(lines1)} рядків)")
    report.append(f"- **Другий файл (порівняння)**: `{args.file2}` ({len(lines2)} рядків)")
    report.append("")

    # Statistics
    added = sum(1 for l in diff if l.startswith('+') and not l.startswith('+++'))
    removed = sum(1 for l in diff if l.startswith('-') and not l.startswith('---'))
    report.append("## Статистика змін")
    report.append(f"- Додано рядків: **{added}**")
    report.append(f"- Вилучено рядків: **{removed}**")
    report.append("")

    report.append("## Виявлені зміни")
    report.append("")
    
    if not diff:
        report.append("Документи ідентичні (змін не виявлено).")
    else:
        report.append("```diff")
        for line in diff:
            report.append(line)
        report.append("```")
        
    try:
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write("\n".join(report))
        print(f"Success! Comparison Markdown report written to: {args.output}")
    except OSError as e:
        print(f"Error writing output to {args.output}: {e}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Terms command
# ---------------------------------------------------------------------------

# Patterns for extracting quoted terms (Ukrainian and standard quotes)
QUOTED_TERM_RE = re.compile(r'[«"""]([^»"""]{2,60})[»"""]')

# Patterns for formal definitions
DEFINITION_PATTERNS = [
    # Term — це ...; Term — означає ...
    re.compile(
        r'[«"""]([^»"""]{2,50})[»"""]\s*(?:—|-)\s*(?:це|означає|є)\s+(.{10,})',
        re.IGNORECASE,
    ),
    # Bullet definitions: • Term — ...
    re.compile(
        r'^[•\-\s]*[«"""]?([^»"""\n]{2,40})[»"""]?\s*(?:—|-)\s+(.{10,})',
    ),
]

# Regex for words that look like defined terms (capitalized multi-word or
# specific formatting patterns common in Ukrainian legal documents)
TERM_USAGE_RE = re.compile(
    r'\b([А-ЯІЇЄҐ][а-яіїєґ]+(?:\s+[а-яіїєґ]+){0,3})\b'
)


def cmd_terms(args):
    """Extracts terms in quotes, formal definitions, and flags undefined terms."""
    text = read_file_text(args.file)
    lines = text.splitlines()

    # ---- Extract quoted terms ----
    matches = QUOTED_TERM_RE.findall(text)
    term_counts = {}
    for match in matches:
        cleaned = match.strip()
        if cleaned and len(cleaned) > 1:
            term_counts[cleaned] = term_counts.get(cleaned, 0) + 1
            
    # ---- Extract formal definitions ----
    definitions = []
    defined_terms_lower = set()

    for idx, line in enumerate(lines, 1):
        for pattern in DEFINITION_PATTERNS:
            m = pattern.search(line)
            if m:
                term = m.group(1).strip()
                definition_text = m.group(2).strip()
                definitions.append({
                    "line": idx,
                    "term": term,
                    "definition": definition_text[:200],
                    "raw_line": line.strip()[:300],
                })
                defined_terms_lower.add(term.lower())
                break

    # ---- Find potentially undefined terms ----
    # Terms that appear in quotes but have no corresponding definition
    undefined_terms = []
    for term, count in term_counts.items():
        if term.lower() not in defined_terms_lower and count >= 2:
            undefined_terms.append({
                "term": term,
                "occurrences": count,
                "note": "Термін вживається у тексті, але визначення не знайдено.",
            })

    # Sort by occurrence count descending
    undefined_terms.sort(key=lambda x: x["occurrences"], reverse=True)

    output_data = {
        "quoted_terms_found": [
            {"term": t, "count": c} 
            for t, c in sorted(term_counts.items(), key=lambda x: x[1], reverse=True)
        ],
        "extracted_definitions": definitions,
        "potentially_undefined_terms": undefined_terms,
        "summary": {
            "total_quoted_terms": len(term_counts),
            "total_definitions_found": len(definitions),
            "total_potentially_undefined": len(undefined_terms),
        },
    }
    
    write_json_output(output_data, args.output)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def write_json_output(data, output_file):
    """Writes data to a JSON file format."""
    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"Success! Data written to: {output_file}")
    except (OSError, TypeError) as e:
        print(f"Error writing to file {output_file}: {e}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=(
            "CLI Helper tool for legal department workflows at the "
            "State Space Agency of Ukraine (ДКА України)."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # Subcommand: scan
    p_scan = subparsers.add_parser(
        "scan",
        help=(
            "Scan a document for placeholders, TODOs, formatting errors, "
            "missing requisites, and outdated law references."
        ),
    )
    p_scan.add_argument("--file", required=True, help="Path to input .docx or .txt file")
    p_scan.add_argument("--output", required=True, help="Path to output JSON file")
    
    # Subcommand: compare
    p_compare = subparsers.add_parser(
        "compare",
        help="Compare two documents and output a detailed Markdown diff.",
    )
    p_compare.add_argument("--file1", required=True, help="Path to base file (DOCX/TXT)")
    p_compare.add_argument("--file2", required=True, help="Path to modified file (DOCX/TXT)")
    p_compare.add_argument("--output", required=True, help="Path to output Markdown report file")
    
    # Subcommand: terms
    p_terms = subparsers.add_parser(
        "terms",
        help=(
            "Extract capitalized terms in quotes, formal definitions, "
            "and flag potentially undefined terms."
        ),
    )
    p_terms.add_argument("--file", required=True, help="Path to input .docx or .txt file")
    p_terms.add_argument("--output", required=True, help="Path to output JSON file")
    
    args = parser.parse_args()
    
    if args.command == "scan":
        cmd_scan(args)
    elif args.command == "compare":
        cmd_compare(args)
    elif args.command == "terms":
        cmd_terms(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
