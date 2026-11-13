import unittest

from skill_review import strict_json as sj


class StrictJsonTests(unittest.TestCase):
    def code(self, data):
        with self.assertRaises(sj.StrictJSONError) as cm:
            sj.parse_object(data)
        return cm.exception.code

    def test_valid_object(self):
        self.assertEqual(sj.parse_object('{"a": 1}'), {"a": 1})
        self.assertEqual(sj.parse_object(b'{"a": 1}\n'), {"a": 1})  # trailing JSON whitespace is fine

    def test_duplicate_keys_top_level_nested_and_escaped(self):
        self.assertEqual(self.code('{"a":1,"a":2}'), "DUPLICATE_KEY")
        self.assertEqual(self.code('{"x":{"a":1,"a":1}}'), "DUPLICATE_KEY")
        self.assertEqual(self.code('{"a":1,"\\u0061":2}'), "DUPLICATE_KEY")

    def test_non_finite_and_overflow(self):
        for lit in ("NaN", "Infinity", "-Infinity", "1e999", "-1e999"):
            self.assertEqual(self.code('{"a":%s}' % lit), "NON_FINITE_NUMBER", lit)

    def test_huge_numbers(self):
        self.assertEqual(self.code('{"a":%s}' % ("9" * 19)), "NUMBER_TOO_LARGE")
        self.assertEqual(self.code('{"a":-%s}' % ("9" * 19)), "NUMBER_TOO_LARGE")
        self.assertEqual(sj.parse_object('{"a":%s}' % ("9" * 18)), {"a": 10**18 - 1})
        self.assertEqual(self.code('{"a":0.%s1}' % ("0" * 40)), "NUMBER_TOO_LARGE")

    def test_trailing_and_leading_garbage(self):
        for text in ('{"a":1} x', 'x {"a":1}', '{"a":1}{"b":2}', '{"a":1}\n{"b":2}', "```json\n{}\n```",
                     "﻿{}", "\f{}", "{} \x0b"):
            self.assertEqual(self.code(text), "NOT_STRICT_JSON", repr(text))

    def test_top_level_must_be_object(self):
        for text in ("[]", "null", '"x"', "1", "true"):
            self.assertEqual(self.code(text), "NOT_A_JSON_OBJECT", text)

    def test_empty(self):
        for text in ("", " ", "\n\t\r "):
            self.assertEqual(self.code(text), "EMPTY_INPUT")

    def test_size_limits_characters_and_bytes(self):
        self.assertEqual(self.code(" " * 4097), "INPUT_TOO_LARGE")
        self.assertEqual(self.code(b" " * 4097), "INPUT_TOO_LARGE")
        self.assertEqual(self.code('"' + "é" * 2100 + '"'), "INPUT_TOO_LARGE")  # 2102 chars, > 4096 bytes

    def test_encoding(self):
        self.assertEqual(self.code(b'{"a":"\xff"}'), "INVALID_ENCODING")
        self.assertEqual(self.code("{\"a\":\"\ud800\"}"), "INVALID_ENCODING")

    def test_input_type(self):
        for bad in (None, 5, [], {}, bytearray(b"{}"), object()):
            self.assertEqual(self.code(bad), "INPUT_NOT_TEXT")

    def test_str_subclass_rejected(self):
        class S(str):
            pass

        self.assertEqual(self.code(S("{}")), "INPUT_NOT_TEXT")

    def test_nesting(self):
        self.assertEqual(self.code("[" * 1500 + "]" * 1500), "NESTING_TOO_DEEP")
        self.assertEqual(self.code('{"a":' * 500 + "1" + "}" * 500), "NESTING_TOO_DEEP")
        self.assertEqual(self.code('{"a":{"b":{"c":{}}}}'), "NESTING_TOO_DEEP")
        self.assertEqual(sj.parse_object('{"a":{"b":{}}}'), {"a": {"b": {}}})  # depth 3 is the limit

    def test_syntax_errors(self):
        for text in ("{'a':1}", '{"a":1,}', "{a:1}", '{"a":1 // c\n}', "{\"a\":\"x\ny\"}"):
            self.assertEqual(self.code(text), "NOT_STRICT_JSON", repr(text))


if __name__ == "__main__":
    unittest.main()
