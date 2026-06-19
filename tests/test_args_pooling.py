"""Tests for argument pooling utilities."""

import unittest
from app.args_pooling import (
    parse_args_tokens,
    _normalize_flag,
    parse_pool_flags,
    extract_flag_values,
    pool_key_for_args_by_flags,
)


class ParseArgsTokensTest(unittest.TestCase):
    def test_none_returns_empty(self):
        self.assertEqual(parse_args_tokens(None), [])

    def test_empty_returns_empty(self):
        self.assertEqual(parse_args_tokens(''), [])

    def test_whitespace_returns_empty(self):
        self.assertEqual(parse_args_tokens('   '), [])

    def test_single_token(self):
        self.assertEqual(parse_args_tokens('foo'), ['foo'])

    def test_multiple_tokens(self):
        self.assertEqual(parse_args_tokens('foo bar baz'), ['foo', 'bar', 'baz'])

    def test_quoted_values_survive(self):
        self.assertEqual(
            parse_args_tokens('--flag "value with spaces"'),
            ['--flag', 'value with spaces'],
        )

    def test_flag_equals_value(self):
        self.assertEqual(
            parse_args_tokens('--res=1920x1080'),
            ['--res=1920x1080'],
        )

    def test_handles_broken_shlex_fallback(self):
        # shlex handles this fine, but if it throws, whitespace split takes over
        self.assertIsInstance(parse_args_tokens('a b'), list)


class NormalizeFlagTest(unittest.TestCase):
    def test_none_returns_empty(self):
        self.assertEqual(_normalize_flag(None), '')

    def test_empty_returns_empty(self):
        self.assertEqual(_normalize_flag(''), '')

    def test_whitespace_returns_empty(self):
        self.assertEqual(_normalize_flag('  '), '')

    def test_adds_double_dash(self):
        self.assertEqual(_normalize_flag('foo'), '--foo')

    def test_preserves_double_dash(self):
        self.assertEqual(_normalize_flag('--foo'), '--foo')

    def test_preserves_single_dash(self):
        self.assertEqual(_normalize_flag('-f'), '-f')

    def test_strips_whitespace(self):
        self.assertEqual(_normalize_flag('  foo  '), '--foo')


class ParsePoolFlagsTest(unittest.TestCase):
    def test_none_returns_empty(self):
        self.assertEqual(parse_pool_flags(None), [])

    def test_empty_returns_empty(self):
        self.assertEqual(parse_pool_flags(''), [])

    def test_single_flag(self):
        self.assertEqual(parse_pool_flags('foo'), ['--foo'])

    def test_comma_separated(self):
        self.assertEqual(
            parse_pool_flags('foo,bar'),
            ['--foo', '--bar'],
        )

    def test_newline_separated(self):
        self.assertEqual(
            parse_pool_flags('foo\nbar\nbaz'),
            ['--foo', '--bar', '--baz'],
        )

    def test_mixed_separators(self):
        self.assertEqual(
            parse_pool_flags('foo,bar\nbaz'),
            ['--foo', '--bar', '--baz'],
        )

    def test_preserves_double_dash_input(self):
        self.assertEqual(parse_pool_flags('--foo'), ['--foo'])

    def test_de_duplicates(self):
        self.assertEqual(
            parse_pool_flags('foo,foo,bar'),
            ['--foo', '--bar'],
        )

    def test_skips_empty_parts(self):
        self.assertEqual(
            parse_pool_flags('foo,,bar'),
            ['--foo', '--bar'],
        )


class ExtractFlagValuesTest(unittest.TestCase):
    def test_none_arg_returns_empty(self):
        self.assertEqual(extract_flag_values(None, ['foo']), [])

    def test_empty_arg_returns_empty(self):
        self.assertEqual(extract_flag_values('', ['foo']), [])

    def test_no_flags_returns_empty(self):
        self.assertEqual(extract_flag_values('--foo bar', []), [])

    def test_flag_equals_value(self):
        self.assertEqual(
            extract_flag_values('--res=1920x1080', ['res']),
            ['1920x1080'],
        )

    def test_flag_space_value(self):
        self.assertEqual(
            extract_flag_values('--res 1920x1080', ['res']),
            ['1920x1080'],
        )

    def test_short_flag_concat_value(self):
        self.assertEqual(
            extract_flag_values('-r1920x1080', ['-r']),
            ['1920x1080'],
        )

    def test_short_flag_space_value(self):
        self.assertEqual(
            extract_flag_values('-r 1920x1080', ['-r']),
            ['1920x1080'],
        )

    def test_skips_flag_with_no_value(self):
        self.assertEqual(
            extract_flag_values('--res', ['res']),
            [],
        )

    def test_flag_then_flag_no_value(self):
        """--flag followed by another flag means missing value."""
        self.assertEqual(
            extract_flag_values('--res --other', ['res']),
            [],
        )

    def test_multiple_flags_extracted(self):
        self.assertEqual(
            extract_flag_values('--res=1080p --api=vulkan', ['res', 'api']),
            ['1080p', 'vulkan'],
        )

    def test_filtered_by_pool_flags_only(self):
        """Only values for specified flags are returned."""
        self.assertEqual(
            extract_flag_values('--res=1080p --api=vulkan', ['res']),
            ['1080p'],
        )

    def test_case_insensitive_flag_matching(self):
        self.assertEqual(
            extract_flag_values('--RES=1080p', ['res']),
            ['1080p'],
        )

    def test_multiple_occurrences_same_flag(self):
        self.assertEqual(
            extract_flag_values('--res=1080p --res=1440p', ['res']),
            ['1080p', '1440p'],
        )


class PoolKeyForArgsByFlagsTest(unittest.TestCase):
    def test_none_arg_returns_none(self):
        self.assertIsNone(pool_key_for_args_by_flags(None, ['foo']))

    def test_no_pool_flags_returns_none(self):
        self.assertIsNone(pool_key_for_args_by_flags('--foo bar', []))

    def test_removes_flag_equals_value(self):
        self.assertEqual(
            pool_key_for_args_by_flags('--res=1080p --api=vulkan', ['res']),
            '--api=vulkan',
        )

    def test_removes_flag_space_value(self):
        self.assertEqual(
            pool_key_for_args_by_flags('--res 1080p --api vulkan', ['res']),
            '--api vulkan',
        )

    def test_removes_short_flag_concat(self):
        self.assertEqual(
            pool_key_for_args_by_flags('-r1080p', ['-r']),
            '<pooled>',
        )

    def test_removes_multiple_flags(self):
        self.assertEqual(
            pool_key_for_args_by_flags('--res=1080p --api=vulkan --other=foo',
                                        ['res', 'api']),
            '--other=foo',
        )

    def test_all_removed_returns_pooled(self):
        self.assertEqual(
            pool_key_for_args_by_flags('--res=1080p', ['res']),
            '<pooled>',
        )

    def test_keeps_non_matching_flags(self):
        self.assertEqual(
            pool_key_for_args_by_flags('--res=1080p --other=foo', ['api']),
            '--res=1080p --other=foo',
        )

    def test_flag_value_that_is_next_flag_skipped(self):
        """--flag followed by another flag should not consume the second flag as value."""
        self.assertEqual(
            pool_key_for_args_by_flags('--res --other', ['res']),
            '--other',
        )

    def test_case_insensitive_removal(self):
        self.assertEqual(
            pool_key_for_args_by_flags('--RES=1080p', ['res']),
            '<pooled>',
        )

    def test_realistic_vulkan_resolution(self):
        """Realistic scenario: pool by API, keep resolution."""
        self.assertEqual(
            pool_key_for_args_by_flags('--resolution 1920x1080 --api vulkan',
                                        ['resolution']),
            '--api vulkan',
        )

    def test_realistic_resolution_pool(self):
        """Pool by resolution, keep API."""
        self.assertEqual(
            pool_key_for_args_by_flags('--resolution 1920x1080 --api vulkan',
                                        ['api']),
            '--resolution 1920x1080',
        )


if __name__ == "__main__":
    unittest.main()
