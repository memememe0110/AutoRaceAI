import sqlite3, tempfile, unittest
from pathlib import Path
from tools.reproduce_issue31_gen675_cleanup import digest_rows, integrity, ro, table_exists

class ReproductionHelpersTests(unittest.TestCase):
    def test_digest_rows_is_stable_and_sensitive_to_content(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"x.sqlite3"
            c=sqlite3.connect(p)
            c.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT, blob BLOB)")
            c.execute("INSERT INTO sample VALUES (1,'a',?)",(b'payload',))
            c.commit()
            first=digest_rows(c,"sample")
            second=digest_rows(c,"sample")
            self.assertEqual(first,second)
            c.execute("UPDATE sample SET value='b' WHERE id=1")
            c.commit()
            self.assertNotEqual(first["sha256"],digest_rows(c,"sample")["sha256"])
            c.close()

    def test_readonly_connection_and_integrity(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/"x.sqlite3"
            c=sqlite3.connect(p)
            c.execute("CREATE TABLE sample (id INTEGER)")
            c.commit(); c.close()
            r=ro(p)
            self.assertEqual(integrity(r),"ok")
            self.assertTrue(table_exists(r,"sample"))
            with self.assertRaises(sqlite3.OperationalError):
                r.execute("INSERT INTO sample VALUES (1)")
            r.close()

if __name__ == "__main__":
    unittest.main()
