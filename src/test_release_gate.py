"""The transmission gate as a **release filter** —— one rule for every exit.

Written fail-first on 2026-10-03 after an adversarial review reproduced, with synthetic rows,
five ways derived text from `no_llm` documents still reached the model:
  · `events` (verbatim fragments) survived a redact —— `gate()` blanked description and timeline only
  · `resolve()` returned partial-match candidates (name · degree · doc_count) **before** the gate
  · `kal_neighbors` kept the relation body of a mixed-provenance relation, dropping only blocked ids
  · `first_seen` / `last_seen` / `degree` were sums over blocked sources too
  · the filter switched off when `blocked()` was empty, so fragments whose source document had
    since disappeared (`doc_id = None`) passed

These tests need no database: the policy is a pure function in `schema_v3`.
"""
import importlib
import json
import unittest

schema = importlib.import_module("schema_v3")
release_filter, gate_candidates, REDACTED = schema.release_filter, schema.gate_candidates, schema.REDACTED


def _entity(doc_ids, events, **extra):
    row = {"entity_id": 1, "name": "x", "type": "concept", "description": "a sentence",
           "degree": 7, "first_seen": "2026-01-01", "last_seen": "2026-09-01",
           "timeline": json.dumps([{"at": "2026-05-01", "change": "c"}]),
           "doc_ids": list(doc_ids), "events": json.dumps(events)}
    row.update(extra)
    return row


EV = [{"at": "2026-02-01", "doc_id": 10, "rev": "current", "text": "from an open doc"},
      {"at": "2026-03-01", "doc_id": 11, "rev": "current", "text": "from a blocked doc"},
      {"at": "2026-04-01", "doc_id": None, "rev": "superseded", "text": "source gone"}]


class RedactTests(unittest.TestCase):
    def test_premise_fixture_has_both_kinds(self):
        # Without this the filter tests below could pass with no filter at all (nothing to drop).
        ids = [e["doc_id"] for e in EV]
        self.assertIn(11, ids); self.assertIn(10, ids); self.assertIn(None, ids)

    def test_redact_drops_blocked_and_sourceless_events_and_reserialises(self):
        row, v = release_filter(_entity([10, 11], EV), {11})
        self.assertEqual(v, "redact")
        ev = json.loads(row["events"])                       # still a JSON string column
        self.assertEqual([e["doc_id"] for e in ev], [10])    # blocked **and** None gone, one left
        self.assertEqual(row["description"], REDACTED)
        self.assertEqual(row["timeline"], "[]")

    def test_redact_recomputes_dates_from_allowed_events_and_hides_degree(self):
        row, _ = release_filter(_entity([10, 11], EV), {11})
        self.assertEqual((row["first_seen"], row["last_seen"]), ("2026-02-01", "2026-02-01"))
        self.assertIsNone(row["degree"])
        self.assertEqual(row["doc_ids"], [10])

    def test_redact_without_surviving_events_blanks_dates(self):
        row, _ = release_filter(_entity([10, 11], [EV[1], EV[2]]), {11})
        self.assertEqual((row["first_seen"], row["last_seen"]), ("", ""))
        self.assertEqual(json.loads(row["events"]), [])

    def test_block_when_every_source_blocked_or_unknown(self):
        self.assertEqual(release_filter(_entity([11], EV), {11}), (None, "block"))
        self.assertEqual(release_filter(_entity([], EV), {11}), (None, "block"))

    def test_pass_still_strips_sourceless_events_while_a_block_is_in_force(self):
        row, v = release_filter(_entity([10], EV), {11})
        self.assertEqual(v, "pass")
        self.assertEqual([e["doc_id"] for e in json.loads(row["events"])], [10])
        self.assertEqual(row["description"], "a sentence")   # body untouched on pass

    def test_nothing_blocked_returns_the_row_itself(self):
        e = _entity([10, 11], EV)
        row, v = release_filter(e, set())
        self.assertIs(row, e); self.assertEqual(v, "pass")

    def test_relation_redact_blanks_keywords_and_timeline(self):
        r = {"src_id": 1, "tgt_id": 2, "doc_ids": [10, 11], "description": "rel body",
             "keywords": ["k1", "k2"], "timeline": json.dumps([{"at": "2026-05-01", "change": "c"}])}
        row, v = release_filter(r, {11})
        self.assertEqual(v, "redact")
        self.assertEqual(row["description"], REDACTED)
        self.assertEqual(row["keywords"], [])
        self.assertEqual(row["timeline"], "[]")
        self.assertEqual(row["doc_ids"], [10])

    def test_unparseable_events_fail_closed(self):
        e = _entity([10, 11], EV); e["events"] = "{not json"
        row, _ = release_filter(e, {11})
        self.assertEqual(row["events"], "[]")


class SafeNameTests(unittest.TestCase):
    def test_control_characters_and_length_are_bounded(self):
        s = schema.safe_name("ok name\nIGNORE PREVIOUS\x1b[31m" + "x" * 500)
        self.assertNotIn("\n", s); self.assertNotIn("\x1b", s); self.assertLessEqual(len(s), schema.NAME_CAP)
        self.assertTrue(s.startswith("ok nameIGNORE PREVIOUS"))

    def test_unicode_names_pass_and_non_strings_vanish(self):
        self.assertEqual(schema.safe_name("옵시디언 · kal"), "옵시디언 · kal")
        self.assertEqual(schema.safe_name(None), "")

    def test_candidates_are_sanitised(self):
        out = schema.gate_candidates([{"name": "a\nb", "type": "t", "degree": 1, "doc_ids": [1]}], set())
        self.assertEqual(out[0]["name"], "ab")


class CandidateTests(unittest.TestCase):
    def test_blocked_candidate_is_dropped_and_redacted_one_loses_counts(self):
        rows = [{"name": "open", "type": "t", "degree": 5, "doc_ids": [10]},
                {"name": "mixed", "type": "t", "degree": 9, "doc_ids": [10, 11]},
                {"name": "secret", "type": "t", "degree": 99, "doc_ids": [11]},
                {"name": "orphan", "type": "t", "degree": 3, "doc_ids": []}]
        out = gate_candidates(rows, {11})
        self.assertEqual([c["name"] for c in out], ["open", "mixed"])
        self.assertEqual(out[0]["degree"], 5); self.assertEqual(out[0]["doc_count"], 1)
        self.assertIsNone(out[1]["degree"]); self.assertIsNone(out[1]["doc_count"])

    def test_nothing_blocked_keeps_everything(self):
        rows = [{"name": "a", "type": "t", "degree": 1, "doc_ids": []}]
        self.assertEqual(gate_candidates(rows, set())[0]["doc_count"], 0)


if __name__ == "__main__":
    unittest.main()
