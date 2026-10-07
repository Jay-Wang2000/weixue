"""Host tests for firmware/tools/usb_provision.py NVS CSV generation (stdlib unittest).

The image round-trip and partition-table tests need the vendored ESP-IDF and its python env and
are skipped without them.
"""
import csv
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import usb_provision as up  # noqa: E402

E = up.Entry


def rows(text):
    return list(csv.DictReader(io.StringIO(text)))


def idf_ready():
    try:
        up.idf_python()
    except up.ProvisionError:
        return False
    return (up.IDF_PATH / "components/nvs_flash/nvs_partition_tool/nvs_parser.py").exists()


EXISTING = [
    E("wifi", "ssid", "string", "Home"),
    E("wifi", "password", "string", "home-pass"),
    E("wifi", "channel", "u8", 11),
    E("wifi", "ssid1", "string", "Office"),
    E("wifi", "password1", "string", "office-pass"),
    E("wifi", "ota_url", "string", "http://192.168.8.220:8003/xiaozhi/ota/"),
    E("wifi", "sleep_mode", "u8", 1),
    E("websocket", "url", "string", "ws://192.168.8.220:8000/xiaozhi/v1/"),
    E("audio", "output_volume", "i32", 70),
    E("nvs.net80211", "sta.ssid", "blob", b"\x04\x00\x00\x00Home"),
    E("phy", "cal_data", "blob", bytes(range(256)) * 8),
]


class InputsTest(unittest.TestCase):
    def test_valid(self):
        inputs, warnings = up.read_inputs({"WIFI_SSID": "Lab", "WIFI_PASSWORD": "secret-pass",
                                           "OTA_URL": "http://10.0.0.2:8003/xiaozhi/ota/"})
        self.assertEqual(inputs, up.Inputs("Lab", "secret-pass", "http://10.0.0.2:8003/xiaozhi/ota/"))
        self.assertEqual(warnings, [])

    def test_unset_and_empty_ota_url(self):
        self.assertIsNone(up.read_inputs({})[0].ota_url)
        self.assertEqual(up.read_inputs({"OTA_URL": ""})[0].ota_url, "")

    def test_rejects(self):
        bad = [
            {"WIFI_SSID": "x" * 33, "WIFI_PASSWORD": "12345678"},
            {"WIFI_SSID": "", "WIFI_PASSWORD": "12345678"},
            {"WIFI_SSID": "Lab"},
            {"WIFI_PASSWORD": "12345678"},
            {"WIFI_SSID": "Lab", "WIFI_PASSWORD": "p" * 65},
            {"OTA_URL": "ftp://10.0.0.2/ota/"},
            {"OTA_URL": "http://" + "a" * 260},
        ]
        for env in bad:
            with self.subTest(env=sorted(env)):
                with self.assertRaises(up.ProvisionError):
                    up.read_inputs(env)

    def test_errors_never_contain_values(self):
        with self.assertRaises(up.ProvisionError) as cm:
            up.read_inputs({"WIFI_SSID": "Lab", "WIFI_PASSWORD": "topsecret" * 8})
        self.assertNotIn("topsecret", str(cm.exception))

    def test_short_password_warns(self):
        self.assertTrue(up.read_inputs({"WIFI_SSID": "Lab", "WIFI_PASSWORD": "short"})[1])
        self.assertEqual(up.read_inputs({"WIFI_SSID": "Lab", "WIFI_PASSWORD": ""})[1], [])


class MergeTest(unittest.TestCase):
    def merged(self, existing=EXISTING, **kw):
        inputs = up.Inputs(kw.get("ssid"), kw.get("password"), kw.get("ota_url"))
        return up.merge(existing, inputs)

    def test_new_ssid_first_and_others_kept(self):
        merged, dropped = self.merged(ssid="Lab", password="lab-pass")
        self.assertEqual(up.saved_ssids(merged),
                         [("Lab", "lab-pass", 0), ("Home", "home-pass", 11), ("Office", "office-pass", 0)])
        self.assertEqual([e.namespace for e in dropped], ["nvs.net80211"])
        m = up.as_map(merged)
        self.assertEqual(m[("wifi", "ota_url")][1], "http://192.168.8.220:8003/xiaozhi/ota/")
        for key in [("wifi", "sleep_mode"), ("websocket", "url"), ("audio", "output_volume"), ("phy", "cal_data")]:
            self.assertIn(key, m)
        self.assertNotIn(("nvs.net80211", "sta.ssid"), m)

    def test_existing_ssid_moves_to_front_with_new_password(self):
        merged, _ = self.merged(ssid="Office", password="new-pass")
        self.assertEqual(up.saved_ssids(merged),
                         [("Office", "new-pass", 0), ("Home", "home-pass", 11)])
        self.assertNotIn(("wifi", "ssid2"), up.as_map(merged))

    def test_list_capped_at_ten(self):
        existing = []
        for i in range(10):
            ks, kp, kc = up.ssid_keys(i)
            existing += [E("wifi", ks, "string", f"net{i}"), E("wifi", kp, "string", f"pass{i}"),
                         E("wifi", kc, "u8", i + 1)]
        merged, _ = self.merged(existing, ssid="Lab", password="lab-pass")
        ssids = up.saved_ssids(merged)
        self.assertEqual(len(ssids), 10)
        self.assertEqual(ssids[0], ("Lab", "lab-pass", 0))
        self.assertEqual(ssids[-1], ("net8", "pass8", 9))

    def test_unpaired_ssid_is_skipped_like_load_from_nvs(self):
        existing = [E("wifi", "ssid", "string", "NoPass"), E("wifi", "ssid3", "string", "Three"),
                    E("wifi", "password3", "string", "three-pass")]
        self.assertEqual(up.saved_ssids(existing), [("Three", "three-pass", 0)])
        merged, _ = self.merged(existing, ssid="Lab", password="")
        self.assertEqual(up.saved_ssids(merged), [("Lab", "", 0), ("Three", "three-pass", 0)])
        self.assertNotIn(("wifi", "ssid3"), up.as_map(merged))

    def test_ota_only_keeps_ssid_list(self):
        merged, _ = self.merged(ota_url="http://10.0.0.2:8003/xiaozhi/ota/")
        self.assertEqual(up.saved_ssids(merged), up.saved_ssids(EXISTING))
        self.assertEqual(up.as_map(merged)[("wifi", "ota_url")][1], "http://10.0.0.2:8003/xiaozhi/ota/")

    def test_empty_ota_url_clears_override(self):
        merged, _ = self.merged(ota_url="")
        self.assertEqual(up.as_map(merged)[("wifi", "ota_url")], ("string", ""))

    def test_no_inputs_keeps_everything_but_driver_cache(self):
        merged, dropped = self.merged()
        self.assertEqual(up.as_map(merged), up.as_map([e for e in EXISTING if e not in dropped]))

    def test_generator_special_key_outside_driver_namespace_refused(self):
        with self.assertRaises(up.ProvisionError):
            self.merged([E("custom", "sta.ssid", "string", "x")])


class CsvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_layout(self):
        merged, _ = up.merge(EXISTING, up.Inputs("Lab", "lab-pass", "http://10.0.0.2:8003/xiaozhi/ota/"))
        r = rows(up.entries_to_csv(merged, self.dir))
        self.assertEqual(list(r[0].keys()), ["key", "type", "encoding", "value"])
        namespaces = [x["key"] for x in r if x["type"] == "namespace"]
        self.assertEqual(namespaces, ["wifi", "websocket", "audio", "phy"])
        wifi = {x["key"]: x for x in r[1:r.index(next(x for x in r if x["key"] == "websocket"))]}
        self.assertEqual((wifi["ssid"]["encoding"], wifi["ssid"]["value"]), ("string", "Lab"))
        self.assertEqual(wifi["password"]["value"], "lab-pass")
        self.assertEqual((wifi["channel"]["encoding"], wifi["channel"]["value"]), ("u8", "0"))
        self.assertEqual(wifi["ssid1"]["value"], "Home")
        self.assertEqual(wifi["channel1"]["value"], "11")
        self.assertEqual(wifi["ota_url"]["value"], "http://10.0.0.2:8003/xiaozhi/ota/")
        self.assertEqual(wifi["sleep_mode"]["encoding"], "u8")
        cal = next(x for x in r if x["key"] == "cal_data")
        self.assertEqual(cal["encoding"], "base64")
        self.assertEqual(next(x for x in r if x["key"] == "output_volume")["encoding"], "i32")

    def test_awkward_strings_round_trip_through_csv(self):
        values = ['a,b', 'quote"inside', ' leading and trailing ', '#hash', "naïve ✓", ""]
        entries = [E("wifi", f"k{i}", "string", v) for i, v in enumerate(values)]
        r = rows(up.entries_to_csv(entries, self.dir))
        self.assertEqual([x["value"] for x in r[1:]], values)
        self.assertTrue(all(x["type"] == "data" for x in r[1:]))

    def test_multiline_string_uses_file_row(self):
        r = rows(up.entries_to_csv([E("cfg", "note", "string", "line1\n#line2\r\n")], self.dir))
        self.assertEqual((r[1]["type"], r[1]["encoding"]), ("file", "string"))
        path = Path(r[1]["value"])
        self.assertEqual(path.read_bytes(), b"line1\n#line2\r\n")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_primitives(self):
        entries = [E("n", k, k, v) for k, v in
                   [("u8", 255), ("i8", -128), ("u16", 65535), ("i16", -1), ("u32", 2**32 - 1),
                    ("i32", -(2**31)), ("u64", 2**64 - 1), ("i64", -(2**63))]]
        r = rows(up.entries_to_csv(entries, self.dir))
        self.assertEqual([(x["key"], x["encoding"], int(x["value"])) for x in r[1:]],
                         [(e.key, e.kind, e.value) for e in entries])

    def test_bad_keys_refused(self):
        for key in ["k" * 16, "#c", ""]:
            with self.subTest(key=key), self.assertRaises(up.ProvisionError):
                up.entries_to_csv([E("ns", key, "u8", 1)], self.dir)
        with self.assertRaises(up.ProvisionError):
            up.entries_to_csv([E("ns", "b", "blob", b"")], self.dir)


class RedactorTest(unittest.TestCase):
    def test_redacts_inputs_and_bssid(self):
        r = up.Redactor(up.Inputs("Lab", "lab-pass", "http://10.0.0.2:8003/xiaozhi/ota/"))
        line = "Found AP: Lab, BSSID: aa:bb:cc:dd:ee:ff, pw lab-pass, conn 10.0.0.2:8003 url http://10.0.0.2:8003/xiaozhi/ota/"
        out = r(line)
        for secret in ["Lab", "lab-pass", "10.0.0.2", "aa:bb:cc"]:
            self.assertNotIn(secret, out)
        self.assertIn("<OTA_URL>", out)


@unittest.skipUnless(idf_ready(), "vendored ESP-IDF / python env not available")
class IdfTest(unittest.TestCase):
    def test_partition_table_32m(self):
        csv_text = (Path(up.VENDOR) / "xiaozhi-esp32/partitions/v2/32m.csv").read_bytes()
        table = up.parse_partition_table(csv_text)
        self.assertEqual(up.find_nvs(table), (0x3B000, 0xD2000))
        self.assertEqual(up.find_nvs(up.parse_partition_table(table.to_binary())), (0x3B000, 0xD2000))

    def test_image_round_trip(self):
        existing = EXISTING + [
            E("cfg", "multi", "string", "a\nb,c\"d"),
            E("cfg", "bigblob", "blob", bytes(i % 251 for i in range(9000))),
            E("cfg", "neg", "i64", -(2**63)),
            E("cfg", "big", "u64", 2**64 - 1),
            E("cfg", "comma", "string", "x, y"),
        ]
        merged, _ = up.merge(existing, up.Inputs("Lab", 'p,a"ss word', "http://10.0.0.2:8003/xiaozhi/ota/"))
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "nvs.csv"
            csv_path.write_text(up.entries_to_csv(merged, Path(tmp)))
            image = up.generate_image(csv_path, 0xD2000, lambda s: s)
        entries, warnings = up.parse_nvs(image)
        self.assertEqual(warnings, [])
        self.assertEqual(up.as_map(entries), up.as_map(merged))
        self.assertEqual(up.saved_ssids(entries)[0], ("Lab", 'p,a"ss word', 0))

    def test_empty_partition_parses_to_nothing(self):
        self.assertEqual(up.parse_nvs(b"\xff" * 0x3000), ([], []))

    def test_garbage_refused(self):
        with self.assertRaises(up.ProvisionError):
            up.parse_nvs(bytes(range(256)) * 48)


if __name__ == "__main__":
    unittest.main()
