"""Unit tests for llunascope.normalize — name + domain normalization."""

import unittest

from llunascope.normalize import (is_subdomain_of, name_similarity,
                                  normalize_company, root_domain)


class TestNormalizeCompany(unittest.TestCase):
    def test_strips_legal_suffixes(self):
        self.assertEqual(normalize_company("PT Bank Mandiri (Persero) Tbk"),
                         "bank mandiri")

    def test_strips_inc_llc(self):
        self.assertEqual(normalize_company("Yahoo Inc."), "yahoo")

    def test_ampersand(self):
        self.assertIn("and", normalize_company("AT&T").split())

    def test_unicode_folded(self):
        self.assertEqual(normalize_company("Café Ségur"), "cafe segur")


class TestNameSimilarity(unittest.TestCase):
    def test_identical(self):
        self.assertEqual(name_similarity("Yahoo", "Yahoo Inc."), 1.0)

    def test_disjoint(self):
        self.assertEqual(name_similarity("Yahoo", "Bank Mandiri"), 0.0)

    def test_partial(self):
        s = name_similarity("Yahoo Holdings", "Yahoo Japan")
        self.assertGreater(s, 0.0)
        self.assertLess(s, 1.0)

    def test_empty(self):
        self.assertEqual(name_similarity("", "Yahoo"), 0.0)


class TestRootDomain(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(root_domain("www.yahoo.com"), "yahoo.com")

    def test_multi_level_suffix(self):
        self.assertEqual(root_domain("www.chat.bankmandiri.co.id"),
                         "bankmandiri.co.id")

    def test_bare_suffix_is_not_a_domain(self):
        self.assertEqual(root_domain("co.id"), "")

    def test_wildcard_stripped(self):
        self.assertEqual(root_domain("*.aol.com"), "aol.com")

    def test_ip_literals_rejected(self):
        self.assertEqual(root_domain("93.184.216.34"), "")
        self.assertEqual(root_domain("2001:db8::1"), "")

    def test_email_and_uri_rejected(self):
        self.assertEqual(root_domain("admin@yahoo.com"), "")
        self.assertEqual(root_domain("https://yahoo.com/x"), "")

    def test_trailing_dot(self):
        self.assertEqual(root_domain("yahoo.com."), "yahoo.com")

    def test_single_label(self):
        self.assertEqual(root_domain("localhost"), "")


class TestIsSubdomainOf(unittest.TestCase):
    def test_true(self):
        self.assertTrue(is_subdomain_of("mail.yahoo.com", "yahoo.com"))
        self.assertTrue(is_subdomain_of("yahoo.com", "yahoo.com"))

    def test_suffix_collision_false(self):
        self.assertFalse(is_subdomain_of("notyahoo.com", "yahoo.com"))


if __name__ == "__main__":
    unittest.main()
