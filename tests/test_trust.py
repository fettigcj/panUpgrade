# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""A Panorama with a self-signed certificate: the problem is explained, the certificate can be accepted by fingerprint, a different
certificate later is refused, and every screen offers the way out."""

import hashlib
import http.server
import io
import os
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from panupgrade import authform, cli, profiles, trust
from panupgrade.trust import CertificateChanged, CertificateProblem
from panupgrade_live import panorama
from tests.test_auth import FakeServices, Home, keys, screen, typing

HAVE_OPENSSL = shutil.which("openssl") is not None


def make_certificate(folder, name):
    """A throwaway self-signed certificate for localhost, made with the openssl tool; returns (cert path, key path, sha256 hex)."""
    cert, key = Path(folder, f"{name}.pem"), Path(folder, f"{name}.key")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "2",
                    "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"], check=True, capture_output=True)
    der = ssl.PEM_cert_to_DER_cert(cert.read_text(encoding="ascii"))
    return cert, key, hashlib.sha256(der).hexdigest()


class FakePanorama(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        if "keygen" in body:
            reply = '<response status="success"><result><key>KEY-FROM-SERVER</key></result></response>'
        else:
            reply = '<response status="success"><result><system><sw-version>12.1.10</sw-version></system></result></response>'
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.end_headers()
        self.wfile.write(reply.encode())

    def log_message(self, *args):
        pass


def serve(cert, key):
    server = http.server.HTTPServer(("127.0.0.1", 0), FakePanorama)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"127.0.0.1:{server.server_address[1]}"


@unittest.skipUnless(HAVE_OPENSSL, "needs the openssl tool to make a test certificate")
class WithARealSelfSignedServer(Home):
    @classmethod
    def setUpClass(cls):
        cls.certs = tempfile.TemporaryDirectory()
        cls.first = make_certificate(cls.certs.name, "first")
        cls.second = make_certificate(cls.certs.name, "second")

    @classmethod
    def tearDownClass(cls):
        cls.certs.cleanup()

    def start(self, which):
        server, address = serve(which[0], which[1])
        self.addCleanup(server.server_close)              # cleanups run last-in first-out: stop serving, then close
        self.addCleanup(server.shutdown)
        return address

    def request(self, address):
        return urllib.request.Request(f"https://{address}/api/", data=b"type=op")

    def test_an_unaccepted_self_signed_certificate_is_a_problem_that_carries_its_fingerprint(self):
        address = self.start(self.first)
        with self.assertRaises(CertificateProblem) as caught:
            panorama._urlopen(address, self.request(address), True, 5)
        problem = caught.exception
        self.assertIn("self", problem.reason.lower())
        self.assertEqual(problem.sha256, self.first[2])
        self.assertEqual(problem.address, address)
        self.assertIn("cannot be verified", str(problem))

    def test_once_accepted_exactly_that_certificate_works_with_no_other_setting(self):
        address = self.start(self.first)
        trust.pin(address, self.first[2], "self-signed certificate")
        with panorama._urlopen(address, self.request(address), True, 5) as response:
            self.assertIn(b"12.1.10", response.read())

    def test_a_different_certificate_for_the_same_address_is_refused_loudly(self):
        address = self.start(self.second)
        trust.pin(address, self.first[2])
        with self.assertRaises(CertificateChanged) as caught:
            panorama._urlopen(address, self.request(address), True, 5)
        self.assertIn("not the one you accepted", str(caught.exception))
        self.assertIn(trust.pretty(self.first[2]), str(caught.exception))
        self.assertIn(trust.pretty(self.second[2]), str(caught.exception))

    def test_turning_checking_off_still_works_as_before(self):
        address = self.start(self.first)
        with panorama._urlopen(address, self.request(address), False, 5) as response:
            self.assertIn(b"12.1.10", response.read())

    def test_logging_in_and_asking_questions_both_go_through_the_same_check(self):
        address = self.start(self.first)
        with self.assertRaises(CertificateProblem):
            panorama.key_from_login(address, "admin", "pw", True, 5)
        with self.assertRaises(CertificateProblem):
            panorama.PanoramaApi(address, "KEY", True).op("show system info")
        trust.pin(address, self.first[2])
        self.assertEqual(panorama.key_from_login(address, "admin", "pw", True, 5), "KEY-FROM-SERVER")
        self.assertEqual(panorama.PanoramaApi(address, "KEY", True).op("show system info").findtext("result/system/sw-version"), "12.1.10")

    def test_the_sign_in_screen_asks_accepts_and_carries_on_adding_the_account(self):
        address = self.start(self.first)

        class Real(FakeServices):
            def sign_in(self, addr, user, password):
                return panorama.key_from_login(addr, user, password, True, 5)

            def trust(self, addr, sha256, reason):
                trust.pin(addr, sha256, reason)

            def trusted(self, addr):
                return trust.record(addr)
        lab = authform.Entry("lab", address, {}, "", "", str(profiles.root() / "lab" / "credentials.txt"))
        form = authform.Form([lab], "lab", services=Real())
        form.zone = "accounts"
        keys(form, "a", *typing("admin"), "tab", "right", "right", "tab", *typing("their-password"), "tab", "enter")      # sign in now, keep the key
        self.assertIsNotNone(form.prompt)
        text = screen(form)
        self.assertIn("Cannot verify Panorama's certificate", text)
        self.assertIn(trust.pretty(self.first[2])[:30], text)
        self.assertIn("[ Accept risk & continue ]", text)
        self.assertEqual(form.accounts, {})
        keys(form, "enter")                                                      # Accept risk & continue
        self.assertIsNone(form.prompt)
        self.assertEqual(form.accounts, {"admin": {"api_key": "KEY-FROM-SERVER"}})
        self.assertEqual(trust.pinned(address), self.first[2])
        self.assertIn("Trusted certificate", screen(form))


class Prompting(Home):
    def problem(self, sha="ab" * 32):
        return CertificateProblem("192.0.2.1", "self-signed certificate", sha)

    def form(self, **over):
        values = dict(services=Services())
        values.update(over)
        lab = authform.Entry("lab", "192.0.2.1", {"a": "pw"}, "", "", str(profiles.root() / "lab" / "credentials.txt"))
        return authform.Form([lab], "lab", **values)

    def test_cancel_trusts_nothing_and_sends_nothing(self):
        form = self.form()
        retried = []
        form.ask_about_certificate(self.problem(), lambda: retried.append(1))
        keys(form, "right", "enter")
        self.assertIsNone(form.prompt)
        self.assertEqual((retried, form.services.trusted_calls), ([], []))
        self.assertIn("Not trusted", form.message)

    def test_escape_also_cancels(self):
        form = self.form()
        form.ask_about_certificate(self.problem(), lambda: None)
        form.press("esc")
        self.assertIsNone(form.prompt)

    def test_accept_pins_then_tries_the_action_again(self):
        form = self.form()
        retried = []
        form.ask_about_certificate(self.problem(), lambda: retried.append(1))
        form.press("enter")
        self.assertEqual(form.services.trusted_calls, [("192.0.2.1", "ab" * 32, "self-signed certificate")])
        self.assertEqual(retried, [1])

    def test_a_certificate_that_could_not_be_read_cannot_be_accepted(self):
        form = self.form()
        form.ask_about_certificate(self.problem(sha=None), lambda: None)
        self.assertIn("could not be read", screen(form))
        form.press("enter")                                                   # the only choice is Cancel
        self.assertEqual(form.services.trusted_calls, [])
        self.assertIsNone(form.prompt)

    def test_opened_for_a_step_that_hit_the_problem_it_finishes_once_settled(self):
        form = self.form()
        form.exit_after_trust = True
        form.ask_about_certificate(self.problem(), lambda: None)
        self.assertEqual(form.press("enter"), "accept")

    def test_test_sign_in_uses_the_same_prompt(self):
        form = self.form(services=Services(raises=self.problem()))
        form.zone = "accounts"
        form.press("t")
        self.assertIsNotNone(form.prompt)
        form.services.raises = None
        form.press("enter")
        self.assertIn("signed in", form.message)

    def test_forget_removes_the_acceptance_and_says_so(self):
        form = self.form()
        form.zone, form.action = "actions", authform.FORGET
        form.press("enter")
        self.assertIn("Forgot the certificate", form.message)
        form.press("enter")
        self.assertIn("No certificate had been accepted", form.message)

    def test_the_prompt_fits_a_small_window_and_keeps_the_buttons(self):
        form = self.form()
        form.ask_about_certificate(self.problem(), lambda: None)
        for width, height in ((60, 24), (90, 24), (140, 40)):
            lines = form.render(width, height)
            self.assertLessEqual(len(lines), height)
            self.assertIn("[ Save & Exit ]", "".join(t for line in lines for _s, t in line))


class Services(FakeServices):
    def __init__(self, raises=None):
        super().__init__()
        self.trusted_calls, self.raises, self.forgotten = [], raises, False

    def test(self, address, name, secret):
        if self.raises:
            raise self.raises
        return True, f"{name} signed in to {address}."

    def trust(self, address, sha256, reason):
        self.trusted_calls.append((address, sha256, reason))

    def forget(self, address):
        was, self.forgotten = not self.forgotten, True
        return was

    def trusted(self, address):
        return None


class AtTheCommandLine(Home):
    def args(self):
        return cli.parse_args(["report", "--panorama", "192.0.2.1"])

    def problem(self):
        return CertificateProblem("192.0.2.1", "self-signed certificate", "cd" * 32)

    def test_a_terminal_shows_the_fingerprint_and_a_yes_trusts_it_and_tries_again(self):
        calls = []

        def connect():
            calls.append(1)
            if len(calls) == 1:
                raise self.problem()
            return "connected"
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True), \
                mock.patch("builtins.input", return_value="y"):
            self.assertEqual(cli._trusting(self.args(), connect), "connected")
        self.assertIn(trust.pretty("cd" * 32), out.getvalue())
        self.assertEqual(trust.pinned("192.0.2.1"), "cd" * 32)

    def test_a_no_leaves_the_error_and_trusts_nothing(self):
        def connect():
            raise self.problem()
        with redirect_stdout(io.StringIO()), mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True), \
                mock.patch("builtins.input", return_value=""):
            with self.assertRaises(CertificateProblem):
                cli._trusting(self.args(), connect)
        self.assertIsNone(trust.pinned("192.0.2.1"))

    def test_a_script_is_never_asked(self):
        def connect():
            raise self.problem()
        with mock.patch("sys.stdin.isatty", return_value=False), mock.patch("builtins.input", side_effect=AssertionError("must not ask")):
            with self.assertRaises(CertificateProblem):
                cli._trusting(self.args(), connect)

    def test_inside_the_shell_the_problem_is_handed_over_instead_of_asked(self):
        args = self.args()
        args._in_shell = True
        with mock.patch("builtins.input", side_effect=AssertionError("must not ask")):
            with self.assertRaises(CertificateProblem):
                cli._trusting(args, lambda: (_ for _ in ()).throw(self.problem()))
        self.assertEqual(args._certificate_problem.sha256, "cd" * 32)


class TheRecord(Home):
    def test_pin_remember_forget_and_the_look_of_a_fingerprint(self):
        self.assertIsNone(trust.pinned("Pano.Example"))
        trust.pin("Pano.Example", "AB:CD" + "00" * 30, "self-signed")
        self.assertEqual(trust.pinned("pano.example"), "abcd" + "00" * 30)
        self.assertEqual(trust.record("pano.example")["reason"], "self-signed")
        self.assertTrue(trust.forget("pano.example"))
        self.assertFalse(trust.forget("pano.example"))
        self.assertEqual(trust.pretty("abcdef"), "AB:CD:EF")

    def test_a_damaged_file_is_no_trust_at_all(self):
        trust.path().write_text("{broken", encoding="utf-8")
        self.assertEqual(trust.read(), {})


if __name__ == "__main__":
    unittest.main()
