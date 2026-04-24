import re

with open('test.py', 'rb') as f:
    content = f.read().decode('utf-8')

pattern = r'def _parse_subtitles\(raw_list: list\).*?return grouped'

replacement = (
    'def _parse_subtitles(raw_list: list) -> Dict[str, List[str]]:\n'
    '    """\n'
    '    Convert API subtitle list into grouped dict {"English": ["https://..."], ...}.\n'
    '\n'
    '    Handles two API formats from LookMovie2:\n'
    '      - Movies:  {"language": "English", "file": "/storage6/subs/..."}\n'
    '      - Shows:   {"language": "English", "file": [id, id, "lang", "title", "/storage6/.vtt", ...]}\n'
    '                 The list mixes integers, language codes, titles, and actual VTT paths.\n'
    '    """\n'
    '    grouped: Dict[str, List[str]] = {}\n'
    '    for entry in raw_list or []:\n'
    '        lang = entry.get("language", "Unknown")\n'
    '        raw_file = entry.get("file", "")\n'
    '        if not raw_file:\n'
    '            continue\n'
    '        # Normalise to list (shows send a mixed list, movies send a plain string)\n'
    '        paths = raw_file if isinstance(raw_file, list) else [raw_file]\n'
    '        for path in paths:\n'
    '            if not path or not isinstance(path, str):\n'
    '                continue   # skip numeric IDs and None\n'
    '            # Only keep actual subtitle file paths (VTT/SRT/ASS) or full URLs\n'
    '            if path.startswith("http"):\n'
    '                grouped.setdefault(lang, []).append(path)\n'
    '            elif path.startswith("/") and "." in path.split("/")[-1]:\n'
    '                # e.g. /storage8/.../en_abc.vtt  - has a file extension\n'
    '                grouped.setdefault(lang, []).append(BASE_URL + path)\n'
    '            # skip language codes, titles, numeric IDs etc.\n'
    '    return grouped'
)

new_content, n = re.subn(pattern, replacement, content, flags=re.DOTALL)
print(f'Replacements made: {n}')
if n > 0:
    with open('test.py', 'wb') as f:
        f.write(new_content.encode('utf-8'))
    print('Done')
else:
    print('ERROR: pattern not found')
