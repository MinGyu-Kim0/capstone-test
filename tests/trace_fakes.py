"""Offline mocks for independent prose generation and annotation-only calls."""

import json


def subject_text(payload):
    passages = []
    for note in payload['lecture_notes']:
        text = note['summary'] if 'source_ids' in note else note['summary'][:120]
        if text not in passages:
            passages.append(text)
    return '핵심 개념\n\n' + '\n\n'.join(passages)


def subject_draft(payload):
    previous = payload.get('previous_note', {})
    used_parts, used_items = set(), set()
    parts = []
    for part in payload['annotation_layout']['parts']:
        old_part = next((old for old in previous.get('parts', []) if old['title'] == part['title'] and old['id'] not in used_parts), {})
        if old_part:
            used_parts.add(old_part['id'])
        proposed = {'key': part['key'], 'items': []}
        if not payload.get('grounding_only'):
            proposed['id'] = old_part.get('id')
        for item in part['items']:
            sources = []
            for note in payload['lecture_notes']:
                # Tiny fake notes may have several paragraphs: use an exact local excerpt.
                quote = item['text'] if item['text'] in note['summary'] else note['summary'][:120]
                if item['text'] in note['summary'] or quote in item['text']:
                    sources.append({'note_id': note['id'], 'quote': quote[:120]})
            entry = {'key': item['key'], 'sources': sources}
            if not payload.get('grounding_only'):
                old_items = [old for old in previous.get('items', []) if old['id'] not in used_items]
                old = next((old for old in old_items if old['text'] == item['text']), None)
                if old is None:
                    ids = {source['note_id'] for source in sources}
                    candidates = [old for old in old_items if ids and ids == {source['note_id'] for source in old['sources']}]
                    old = candidates[0] if len(candidates) == 1 else None
                entry['id'] = old['id'] if old else None
                if old:
                    used_items.add(old['id'])
            proposed['items'].append(entry)
        parts.append(proposed)
    result = {'parts': parts}
    if not payload.get('grounding_only'):
        result['removed_ids'] = [old['id'] for old in previous.get('items', []) if old['active'] and old['id'] not in used_items]
    return result


def subject_response(payload):
    return json.dumps(subject_draft(payload), ensure_ascii=False) if 'annotation_layout' in payload else subject_text(payload)
