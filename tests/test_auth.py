# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 the panUpgrade contributors

"""Panorama profiles and the sign-in screen: a workspace per Panorama, the admin accounts under each, and what happens when credentials
are missing."""

import argparse
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from panupgrade import authform, cli, credstore, profiles, site, tui
from panupgrade.sources import SourceError

SECRET = "SECRET-VALUE-123"
KEYS = ("PANUPGRADE_HOME", "PANUPGRADE_CREDS", "PANUPGRADE_ACCOUNT", "PANUPGRADE_WORK_ACCOUNT", "PANUPGRADE_PROFILE")


class Home(unittest.TestCase):
    """Every test gets its own sign-in folder, and none of the real PANUPGRADE_* variables."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        env = {k: v for k, v in os.environ.items() if k not in KEYS}
        env["PANUPGRADE_HOME"] = str(self.home)
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        cli._SAID.clear()
        cli._KEYS.clear()

    def creds(self, text):
        path = self.home / "credentials.txt"
        path.write_text(text, encoding="utf-8")
        return path

    def profile(self, name="lab", address="192.0.2.1", accounts=None, **data):
        """A profile on disk, with its credentials file when accounts are given."""
        profile = profiles.create(name, address)
        profile.data.update(data)
        profile.save()
        if accounts is not None:
            credstore.write(profile.creds_file, accounts)
        return profile


class TestsStayOutOfTheRealHome(unittest.TestCase):
    def test_the_process_is_pointed_at_a_throwaway_folder_before_any_test_runs(self):
        real = Path.home().resolve() / ".panupgrade"
        self.assertNotEqual(site.home_dir().resolve(), real)
        self.assertIn("panupgrade-test-home-", str(site.home_dir()))
        self.assertEqual([n for n in os.environ if n.startswith("PANUPGRADE_") and n != "PANUPGRADE_HOME"], [])


class TheProfiles(Home):
    def test_a_profile_is_a_folder_that_lists_itself(self):
        self.assertEqual(profiles.names(), [])
        lab = profiles.create("lab", " 192.0.2.1 ")
        self.assertEqual((lab.name, lab.address), ("lab", "192.0.2.1"))
        self.assertEqual(profiles.names(), ["lab"])
        self.assertEqual(Path(lab.path("upgradeList.txt")), self.home / "profiles" / "lab" / "upgradeList.txt")
        self.assertEqual(Path(lab.creds_file), self.home / "profiles" / "lab" / "credentials.txt")

    def test_names_are_checked_and_not_reused(self):
        for bad in ("", " x", "a/b", "..", "a\\b", "x" * 41, "name.", "old.removed-1"):
            self.assertFalse(profiles.valid_name(bad), bad)
        self.assertTrue(profiles.valid_name("prod east 2"))
        profiles.create("lab")
        with self.assertRaisesRegex(profiles.ProfileError, "already"):
            profiles.create("lab")
        with self.assertRaises(profiles.ProfileError):
            profiles.create("../x")

    def test_removing_a_profile_keeps_its_folder_so_no_backup_or_journal_is_lost(self):
        lab = profiles.create("lab", "192.0.2.1")
        lab.path("backups").mkdir()
        (lab.path("backups") / "config.xml").write_text("<config/>", encoding="utf-8")
        kept = profiles.remove("lab")
        self.assertEqual(profiles.names(), [])
        self.assertTrue((self.home / "profiles" / kept / "backups" / "config.xml").is_file())
        profiles.create("lab")                                      # the name is free again
        self.assertEqual(profiles.names(), ["lab"])
        profiles.remove("lab")                                      # and a second removal does not collide with the first
        self.assertEqual(len([p for p in (self.home / "profiles").iterdir() if ".removed-" in p.name]), 2)

    def test_which_profile_is_used(self):
        a, b = profiles.create("a", "10.0.0.1"), profiles.create("b", "10.0.0.2")
        self.assertIsNone(profiles.choose(address="10.9.9.9"))                       # a Panorama with no profile is used as it is
        self.assertEqual(profiles.choose(address="10.0.0.2").name, "b")              # matched by address
        self.assertEqual(profiles.choose(address="10.0.0.2".upper()).name, "b")
        self.assertIsNone(profiles.choose())                                          # two profiles, none remembered
        profiles.remember("a")
        self.assertEqual(profiles.choose().name, "a")                                 # the one used last
        self.assertEqual(profiles.choose("b").name, "b")                              # explicit wins
        os.environ["PANUPGRADE_PROFILE"] = "b"
        self.assertEqual(profiles.choose().name, "b")                                 # then the environment variable
        self.assertEqual(profiles.choose("a").name, "a")
        self.assertEqual(profiles.choose(address="10.0.0.1").name, "a")               # but naming a Panorama beats it
        self.assertIsNone(profiles.choose(address="10.9.9.9"))                        # and so does naming one that has no profile
        with self.assertRaisesRegex(profiles.ProfileError, "no profile called nope.*profiles: a, b"):
            profiles.choose("nope")
        self.assertEqual((a.address, b.address), ("10.0.0.1", "10.0.0.2"))

    def test_a_single_profile_is_simply_the_one_in_use(self):
        profiles.create("only", "10.0.0.1")
        self.assertEqual(profiles.choose().name, "only")
        self.assertIsNone(profiles.choose(address="10.0.0.9"))

    def test_the_record_holds_names_and_addresses_not_secrets(self):
        lab = self.profile(accounts={"admin": SECRET})
        record = (lab.dir / "profile.json").read_text(encoding="utf-8")
        self.assertNotIn(SECRET, record)
        self.assertEqual(json.loads(record), {"address": "192.0.2.1"})


class WhereTheChoiceComesFrom(Home):
    def test_defaults_when_nothing_is_set(self):
        self.assertEqual(site.creds_path(), (str(self.home / "credentials.txt"), "default"))
        self.assertEqual(site.account(), (None, "default"))

    def test_a_profile_beats_the_default_and_an_environment_variable_beats_the_profile(self):
        lab = self.profile(account="from-profile", work_account="worker")
        self.assertEqual(site.account(lab), ("from-profile", "profile:lab"))
        self.assertEqual(site.work_account(lab), ("worker", "profile:lab"))
        self.assertEqual(site.creds_path(lab), (lab.creds_file, "profile:lab"))
        os.environ["PANUPGRADE_ACCOUNT"] = "from-env"
        os.environ["PANUPGRADE_CREDS"] = "elsewhere.txt"
        self.assertEqual(site.account(lab), ("from-env", "PANUPGRADE_ACCOUNT"))
        self.assertEqual(site.creds_path(lab), ("elsewhere.txt", "PANUPGRADE_CREDS"))

    def test_the_announcement_says_which_profile_the_choice_came_from(self):
        lab = self.profile(accounts={"svc": "S3CR3T-value", "other": "S3CR3T-value"}, account="svc")
        a = argparse.Namespace(source="panorama:192.0.2.1", creds_file=None, account=None, work_account=None, ask_credentials=None,
                               color="never", ascii=True, _profile=lab)
        out = io.StringIO()
        with redirect_stdout(out):
            cli._announce_connection(a)
        text = out.getvalue()
        self.assertIn("Reading as: svc (from the profile lab)", text)
        self.assertIn("Credentials file:", text)
        self.assertIn("from the profile lab", text)
        self.assertNotIn("S3CR3T", text)                    # (not "pw": the random temp folder name can contain it)


class TheCredentialsFile(Home):
    def test_accounts_round_trip_and_the_file_stays_readable_by_hand(self):
        accounts = {"person": "pa'ss\"word", "service": {"api_key": "KEY"}}
        credstore.write(self.home / "sub" / "c.txt", accounts)                          # the folder is created
        self.assertEqual(credstore.read(self.home / "sub" / "c.txt"), accounts)
        self.assertEqual((self.home / "sub" / "c.txt").read_text(encoding="utf-8").splitlines()[0], "{")

    @unittest.skipIf(os.name == "nt", "file modes are not how Windows says who may read a file")
    def test_a_new_file_is_for_its_owner_alone(self):
        credstore.write(self.home / "c.txt", {"a": "b"})
        self.assertEqual(stat.S_IMODE((self.home / "c.txt").stat().st_mode), 0o600)

    def test_missing_is_something_to_fix_and_malformed_is_not_overwritten(self):
        with self.assertRaises(credstore.NeedsSignIn):
            credstore.read(self.home / "none.txt")
        bad = self.creds("{'a': 5}")
        with self.assertRaises(SourceError) as caught:
            credstore.read(bad)
        self.assertNotIsInstance(caught.exception, credstore.NeedsSignIn)

    def test_refuses_to_write_nonsense(self):
        for accounts in ({"": "pw"}, {"a": ""}, {"a": {"api_key": ""}}, {"a": 5}):
            with self.assertRaises(ValueError):
                credstore.write(self.home / "c.txt", accounts)
        self.assertFalse((self.home / "c.txt").exists())


class FakeServices:
    def __init__(self):
        self.saved, self.tested, self.signed_in = [], [], []

    def sign_in(self, address, user, password):
        self.signed_in.append((address, user))
        if password == "wrong":
            raise SourceError(f"{address}: login as {user} failed")
        return "KEY-FROM-LOGIN"

    def test(self, address, name, secret):
        self.tested.append((address, name))
        return True, f"{name} signed in to {address}."

    def save(self, form):
        self.saved.append({n: dict(e.accounts) for n, e in form.entries.items()})
        return authform.save_all(form, set_user_env=lambda *a: None, delete_user_env=lambda *a: None)


def typing(text):
    return list(text)


def keys(form, *presses):
    for p in presses:
        form.press(p)


def screen(form, width=110, height=36):
    return tui.plain(form.render(width, height, "title here"))


def entry(name="lab", address="192.0.2.1", accounts=None, reading="", changes=""):
    return authform.Entry(name, address, accounts or {}, reading, changes, str(profiles.root() / name / "credentials.txt"))


class TheForm(Home):
    def form(self, *entries, active="", **over):
        values = dict(store="state", notice="", services=FakeServices(), overrides=())
        values.update(over)
        return authform.Form(list(entries) or [entry()], active, **values)

    def add(self, form, name, kind, secret):
        form.zone = "accounts"
        keys(form, "a", *typing(name), "tab")
        for _ in range(kind):
            form.press("right")
        keys(form, "tab", *typing(secret), "tab", "enter")

    def test_a_password_account_is_added_to_the_profile_in_use_and_never_shown_again(self):
        form = self.form()
        self.add(form, "person", 0, SECRET)
        self.assertEqual(form.accounts, {"person": SECRET})
        self.assertIn("Added person", form.message)
        text = screen(form)
        self.assertIn("person", text)
        self.assertIn("password", text)
        self.assertNotIn(SECRET, text)

    def test_the_secret_is_masked_while_it_is_typed(self):
        form = self.form()
        form.zone = "accounts"
        keys(form, "a", *typing("person"), "tab", "tab", *typing(SECRET))
        text = screen(form)
        self.assertIn("*" * len(SECRET), text)
        self.assertNotIn(SECRET, text)

    def test_an_api_key_account_stores_the_key(self):
        form = self.form()
        self.add(form, "service", 1, " KEY-1 ")
        self.assertEqual(form.accounts, {"service": {"api_key": "KEY-1"}})
        self.assertIn("API key", screen(form))

    def test_sign_in_now_keeps_only_the_key_and_forgets_the_password(self):
        form = self.form()
        self.add(form, "admin", 2, "their-password")
        self.assertEqual(form.accounts, {"admin": {"api_key": "KEY-FROM-LOGIN"}})
        self.assertEqual(form.services.signed_in, [("192.0.2.1", "admin")])
        self.assertNotIn("their-password", repr(form.accounts))

    def test_a_failed_sign_in_keeps_the_dialog_open_and_adds_nothing(self):
        form = self.form()
        self.add(form, "admin", 2, "wrong")
        self.assertEqual(form.accounts, {})
        self.assertIsNotNone(form.modal)
        self.assertIn("login as admin failed", form.message)

    def test_sign_in_now_needs_the_profiles_address(self):
        form = self.form(entry(address=""))
        self.add(form, "admin", 2, "pw")
        self.assertEqual(form.accounts, {})
        self.assertIn("needs this profile's Panorama address", form.message)

    def test_a_name_and_a_secret_are_required_and_escape_closes_the_dialog(self):
        form = self.form()
        form.zone = "accounts"
        keys(form, "a", "tab", "tab", "tab", "enter")                 # name, kind, secret, then the Add button
        self.assertIn("needs a name", form.message)
        self.assertIsNotNone(form.modal)
        keys(form, *typing("x"), "tab", "tab", "tab", "enter")
        self.assertIn("Type the password", form.message)
        form.press("esc")
        self.assertIsNone(form.modal)

    def test_remove_and_the_choices_for_reading_and_changes(self):
        form = self.form(entry(accounts={"a": "1", "b": "2", "c": "3"}))
        form.zone = "accounts"
        keys(form, "r")                                              # a for reading
        keys(form, "down", "c")                                      # b for changes
        self.assertEqual((form.reading, form.changes), ("a", "b"))
        keys(form, "c")                                              # again: back to the same as reading
        self.assertEqual(form.changes, "")
        keys(form, "c", "delete")                                    # b chosen for changes, then removed
        self.assertEqual((form.account_names(), form.reading, form.changes), (["a", "c"], "a", ""))
        self.assertIn("used for", screen(form))

    def test_the_only_account_is_used_for_everything_without_choosing(self):
        form = self.form(entry(accounts={"solo": "pw"}))
        self.assertEqual((form.effective_reading(), form.effective_changes()), ("solo", "solo"))
        self.assertIn("reading, changes", screen(form))

    def test_test_sign_in_needs_an_address_then_reports(self):
        form = self.form(entry(address="", accounts={"a": "pw"}))
        form.zone = "accounts"
        keys(form, "t")
        self.assertIn("Type this profile's Panorama address first", form.message)
        self.assertEqual(form.zone, "address")
        keys(form, *typing("192.0.2.1"), "tab", "tab", "t")            # address -> profile in use -> accounts
        self.assertEqual(form.services.tested, [("192.0.2.1", "a")])
        self.assertEqual(form.message, "a signed in to 192.0.2.1.")


class FailingServices(FakeServices):
    def test(self, address, name, secret):
        self.tested.append((address, name))
        return False, f"{address}: login as {name} failed (invalid credentials)"


class TheLastSignInTest(Home):
    """Test sign-in answers once and the answer used to vanish at the next key; now the table keeps it."""

    def form(self, services=None, **kw):
        self.profile("lab", "192.0.2.1", {"a": "pw"})
        e = authform.Entry("lab", "192.0.2.1", {"a": "pw"}, "", "", str(profiles.root() / "lab" / "credentials.txt"),
                           sign_ins=profiles.sign_ins("lab"))
        return authform.Form([e], "lab", "state", "", services or FakeServices())

    def test_an_account_that_was_never_tested_says_so(self):
        text = screen(self.form())
        self.assertIn("last sign-in test", text)
        self.assertIn("not tested yet", text)

    def test_a_good_test_stays_in_the_table_with_its_date_and_time_after_the_message_is_gone(self):
        form = self.form()
        form.zone = "accounts"
        keys(form, "t")
        self.assertIn("signed in to", form.message)
        keys(form, "up", "down")                                    # any key clears the message
        self.assertEqual(form.message, "")
        text = screen(form, width=130)
        self.assertRegex(text, r"connected successfully on \d{4}-\d\d-\d\d \d\d:\d\d")
        self.assertNotIn("not tested yet", text)

    def test_a_failed_test_is_shown_as_failed_with_the_reason_underneath(self):
        form = self.form(FailingServices())
        form.zone = "accounts"
        keys(form, "t")
        text = screen(form, width=130)
        self.assertRegex(text, r"failed on \d{4}-\d\d-\d\d \d\d:\d\d")
        self.assertIn("login as a failed (invalid credentials)", text)

    def test_a_narrow_window_shortens_the_wording_but_not_the_facts(self):
        form = self.form()
        form.zone = "accounts"
        keys(form, "t")
        self.assertRegex(screen(form, width=100), r"connected \d{4}-\d\d-\d\d \d\d:\d\d")

    def test_the_result_is_on_disk_at_once_and_comes_back_next_time(self):
        form = self.form(FailingServices())
        form.zone = "accounts"
        keys(form, "t")                                             # nothing saved, nothing even edited
        book = profiles.sign_ins("lab")
        self.assertFalse(book["a"]["ok"])
        again = cli._auth_form(argparse.Namespace(profile="lab", source=None))[0]
        again.zone = "accounts"
        self.assertIn("failed on", screen(again, width=130))

    def test_replacing_an_accounts_secret_forgets_the_old_result(self):
        form = self.form()
        form.zone = "accounts"
        keys(form, "t", "a", *typing("a"), "tab", "tab", *typing("new"), "tab", "enter")
        self.assertIn("not tested yet", screen(form))

    def test_a_new_unsaved_profile_keeps_its_results_until_it_is_saved(self):
        form = authform.Form([], "", "state", "", FakeServices())
        keys(form, *typing("fresh"), "tab", *typing("192.0.2.9"), "tab", "enter")
        self.assertFalse((profiles.root() / "fresh").exists())
        form.zone = "accounts"
        keys(form, "a", *typing("u"), "tab", "tab", *typing("pw"), "tab", "enter")
        keys(form, "t")
        self.assertFalse((profiles.root() / "fresh" / profiles.SIGN_INS).exists())    # not saved yet
        authform.save_all(form)
        self.assertTrue(profiles.sign_ins("fresh")["u"]["ok"])

    def test_removing_an_account_drops_its_result_when_saved(self):
        form = self.form()
        form.zone = "accounts"
        keys(form, "t", "delete")
        authform.save_all(form)
        self.assertEqual(profiles.sign_ins("lab"), {})


class SeveralPanoramas(Home):
    def form(self, *entries, **over):
        values = dict(store="state", notice="", services=FakeServices(), overrides=())
        values.update(over)
        return authform.Form(list(entries), values.pop("active", ""), **values)

    def two(self):
        return self.form(entry("east", "10.0.0.1", {"e-admin": "pw"}, "e-admin"), entry("west", "10.0.0.2", {"w-one": "pw", "w-two": "pw"}))

    def test_left_and_right_switch_profile_and_each_has_its_own_address_and_accounts(self):
        form = self.two()
        self.assertEqual((form.active, form.address, form.account_names()), ("east", "10.0.0.1", ["e-admin"]))
        form.zone = "profile"
        form.press("right")
        self.assertEqual((form.active, form.address, form.account_names()), ("west", "10.0.0.2", ["w-one", "w-two"]))
        form.press("right")
        self.assertEqual(form.active, "east")                          # wraps
        form.press("left")
        self.assertEqual(form.active, "west")
        text = screen(form)
        self.assertIn("< west >", text)
        self.assertIn("2 of 2", text)

    def test_an_account_added_belongs_to_the_profile_in_use_only(self):
        form = self.two()
        form.zone = "accounts"
        keys(form, "a", *typing("new"), "tab", "tab", *typing("pw"), "tab", "enter")
        self.assertEqual(form.entries["east"].accounts, {"e-admin": "pw", "new": "pw"})
        self.assertEqual(form.entries["west"].accounts, {"w-one": "pw", "w-two": "pw"})

    def test_the_same_user_name_can_have_different_secrets_on_different_panoramas(self):
        form = self.form(entry("east", "10.0.0.1", {"admin": "east-pw"}), entry("west", "10.0.0.2", {"admin": "west-pw"}))
        self.assertEqual(form.entries["east"].accounts["admin"], "east-pw")
        self.assertEqual(form.entries["west"].accounts["admin"], "west-pw")

    def test_the_address_is_edited_for_the_profile_in_use(self):
        form = self.two()
        form.zone = "address"
        keys(form, "ctrl-u", *typing("pano.example"))
        self.assertEqual(form.entries["east"].address, "pano.example")
        self.assertEqual(form.entries["west"].address, "10.0.0.2")

    def test_a_new_profile_is_made_from_a_name_and_an_address_and_becomes_the_one_in_use(self):
        form = self.two()
        form.zone = "profile"
        keys(form, "n", *typing("south"), "tab", *typing("10.0.0.3"), "tab", "enter")
        self.assertIsNone(form.modal)
        self.assertEqual((form.active, form.address, form.account_names()), ("south", "10.0.0.3", []))
        self.assertIn("Added the profile south", form.message)
        self.assertTrue(form.dirty())

    def test_a_bad_or_taken_profile_name_is_refused_with_a_reason(self):
        form = self.two()
        form.zone = "profile"
        keys(form, "n", *typing("../x"), "tab", "tab", "enter")
        self.assertIn("starts with a letter or digit", form.message)
        form.press("ctrl-u")
        keys(form, *typing("east"), "tab", "tab", "enter")
        self.assertIn("already a profile called east", form.message)
        self.assertEqual(sorted(form.entries), ["east", "west"])

    def test_the_new_profile_button_works_from_the_action_row_too(self):
        form = self.two()
        form.zone, form.action = "actions", authform.NEW_PROFILE
        form.press("enter")
        self.assertEqual(form.modal["type"], "profile")

    def test_removing_a_profile_hides_it_and_says_its_folder_is_kept(self):
        form = self.two()
        form.zone = "profile"
        form.press("delete")
        self.assertEqual((form.names(), form.active), (["west"], "west"))
        self.assertIn("folder is kept", form.message)

    def test_removing_the_last_profile_goes_straight_to_making_a_new_one(self):
        form = self.form(entry())
        form.zone = "profile"
        form.press("delete")
        self.assertEqual(form.modal["type"], "profile")

    def test_with_no_profiles_at_all_it_starts_on_the_new_profile_dialog(self):
        form = authform.Form([], services=FakeServices(), draft_address="192.0.2.77")
        self.assertEqual(form.modal["type"], "profile")
        self.assertEqual(form.modal["address"], "192.0.2.77")
        self.assertIn("New Panorama profile", screen(form))

    def test_a_panorama_with_no_profile_yet_offers_to_make_one_even_when_others_exist(self):
        form = authform.Form([entry("east", "10.0.0.1")], "east", services=FakeServices(), draft_address="10.9.9.9")
        self.assertEqual(form.modal["address"], "10.9.9.9")
        known = authform.Form([entry("east", "10.0.0.1")], "east", services=FakeServices(), draft_address="10.0.0.1")
        self.assertIsNone(known.modal)

    def test_the_account_actions_say_to_make_a_profile_first_when_there_is_none(self):
        form = authform.Form([], services=FakeServices())
        form.modal = None
        form.zone = "actions"
        form.press("enter")
        self.assertIn("Make a profile first", form.message)

    def test_the_ring_of_zones_wraps(self):
        form = self.two()
        form.zone = "profile"
        seen = []
        for _ in range(7):
            seen.append(form.zone)
            form.press("tab")
        self.assertEqual(seen, ["profile", "address", "store", "accounts", "actions", "buttons", "profile"])
        form.zone = "profile"
        form.press("shift-tab")
        self.assertEqual(form.zone, "buttons")

    def test_unsaved_changes_are_marked_and_escape_asks_twice(self):
        form = self.two()
        self.assertEqual(form.press("esc"), "cancel")
        form = self.two()
        form.zone = "accounts"
        keys(form, "c")                                              # east's account also makes the changes: a difference from what was saved
        self.assertTrue(form.dirty())
        self.assertIn("unsaved changes", screen(form))
        self.assertIsNone(form.press("esc"))
        self.assertEqual(form.press("esc"), "cancel")

    def test_no_line_is_wider_than_the_window_and_the_buttons_stay_visible(self):
        form = self.form(entry(accounts={f"account-{i}": "pw" for i in range(12)}), entry("west", "10.0.0.2"))
        for width, height in ((60, 24), (90, 24), (140, 40)):
            lines = form.render(width, height)
            self.assertLessEqual(len(lines), height)
            for line in lines:
                self.assertLess(len("".join(t for _s, t in line)), width)
            self.assertIn("[ Save & Exit ]", tui.plain(lines))

    def test_every_action_button_is_on_screen_however_narrow_the_window(self):
        form = self.two()
        for width in (160, 100, 70):
            text = tui.plain(form.render(width, 40))
            for label in authform.ACTION_LABELS:
                self.assertIn(f"[ {label} ]", text, (width, label))


class Saving(Home):
    def form(self, *entries, active="", **over):
        values = dict(store="state", services=FakeServices())
        values.update(over)
        return authform.Form(list(entries), active, **values)

    def save(self, form, **kw):
        return authform.save_all(form, set_user_env=kw.get("set", mock.Mock()), delete_user_env=kw.get("delete", mock.Mock()))

    def test_a_new_profile_is_written_with_its_record_its_credentials_and_is_remembered(self):
        form = authform.Form([], services=FakeServices(), draft_address="192.0.2.1")
        keys(form, *typing("lab"), "tab", "tab", "enter")                       # the new profile dialog: name, address (prefilled), Create
        form.zone = "accounts"
        keys(form, "a", *typing("admin"), "tab", "tab", *typing(SECRET), "tab", "enter", "r")
        message = self.save(form)
        self.assertIn("lab (1 account)", message)
        lab = profiles.load("lab")
        self.assertEqual((lab.address, lab.account), ("192.0.2.1", "admin"))
        self.assertEqual(credstore.read(lab.creds_file), {"admin": SECRET})
        self.assertNotIn(SECRET, (lab.dir / "profile.json").read_text(encoding="utf-8"))
        self.assertEqual(profiles.remembered(), "lab")

    def test_several_profiles_are_written_each_to_its_own_folder(self):
        east, west = entry("east", "10.0.0.1", {"a": "1"}), entry("west", "10.0.0.2", {"b": "2"})
        east.new = west.new = True
        self.save(self.form(east, west, active="west"))
        self.assertEqual(credstore.read(profiles.load("east").creds_file), {"a": "1"})
        self.assertEqual(credstore.read(profiles.load("west").creds_file), {"b": "2"})
        self.assertEqual(profiles.names(), ["east", "west"])
        self.assertEqual(profiles.remembered(), "west")

    def test_saving_keeps_what_the_screen_does_not_edit_such_as_the_target_version(self):
        lab = self.profile(accounts={"a": "pw"}, target="10.2.11-h1")
        form = self.form(authform.Entry("lab", lab.address, {"a": "pw"}, "", "", lab.creds_file))
        form.address = "192.0.2.50"
        self.save(form)
        self.assertEqual((profiles.load("lab").address, profiles.load("lab").target), ("192.0.2.50", "10.2.11-h1"))

    def test_removing_a_profile_renames_its_folder_and_never_deletes_it(self):
        lab = self.profile(accounts={"a": "pw"})
        lab.path("backups").mkdir()
        (lab.path("backups") / "kept.xml").write_text("x", encoding="utf-8")
        form = self.form(authform.Entry("lab", lab.address, {"a": "pw"}, "", "", lab.creds_file), entry("other"))
        form.zone = "profile"
        form.active = "lab"
        form.press("delete")
        message = self.save(form)
        self.assertIn("removed lab (folder kept as lab.removed-", message)
        self.assertEqual(profiles.names(), ["other"])
        kept = next(p for p in (self.home / "profiles").iterdir() if p.name.startswith("lab.removed-"))
        self.assertTrue((kept / "backups" / "kept.xml").is_file())
        self.assertTrue((kept / "credentials.txt").is_file())

    def test_a_profile_made_and_dropped_before_saving_leaves_nothing(self):
        form = self.form(entry("keep"))
        form.zone = "profile"
        keys(form, "n", *typing("scratch"), "tab", "tab", "enter")
        form.press("delete")
        self.save(form)
        self.assertNotIn("scratch", profiles.names())

    def test_the_environment_choice_sets_the_user_variable_and_stops_remembering_in_the_file(self):
        lab = self.profile()
        profiles.remember("lab")
        form = self.form(authform.Entry("lab", lab.address, {}, "", "", lab.creds_file), active="lab", store="env")
        set_var, delete_var = mock.Mock(), mock.Mock()
        message = self.save(form, set=set_var, delete=delete_var)
        set_var.assert_called_once_with("PANUPGRADE_PROFILE", "lab")
        self.assertEqual(profiles.remembered(), "")
        self.assertIn("new terminal", message)

    def test_switching_back_to_remembering_clears_the_user_variable_that_would_override_it(self):
        lab = self.profile()
        form = self.form(authform.Entry("lab", lab.address, {}, "", "", lab.creds_file), active="lab", store="state")
        deleted = []
        with mock.patch("panupgrade.userenv.supported", return_value=True), mock.patch("panupgrade.userenv.get", return_value="lab"):
            self.save(form, delete=deleted.append)
        self.assertEqual(deleted, ["PANUPGRADE_PROFILE"])
        self.assertEqual(profiles.remembered(), "lab")

    def test_the_form_saves_through_its_services_and_marks_itself_clean(self):
        form = self.form(entry(accounts={"a": "pw"}))
        form.zone = "accounts"
        keys(form, "r", "tab", "tab")                                 # accounts -> actions -> buttons
        self.assertEqual(form.zone, "buttons")
        self.assertEqual(form.press("enter"), "accept")               # Save & Exit
        self.assertFalse(form.dirty())
        self.assertTrue(form.ever_saved)
        self.assertEqual(profiles.load("lab").account, "a")

    def test_a_failed_save_says_why_and_stays_open(self):
        class Broken(FakeServices):
            def save(self, form):
                raise PermissionError(13, "Permission denied")
        form = self.form(entry(accounts={"a": "pw"}), services=Broken())
        form.zone = "accounts"
        keys(form, "r")
        self.assertIsNone(form.press("ctrl-d"))
        self.assertIn("Could not save: Permission denied", form.message)
        self.assertTrue(form.dirty())

    def test_environment_variables_are_not_offered_where_there_are_none(self):
        form = self.form(entry())
        form.zone = "store"
        with mock.patch("panupgrade.userenv.supported", return_value=False):
            form.press("right")
        self.assertEqual(form.store, "state")
        self.assertIn("no user-level environment variables", form.message)


class MovingAround(Home):
    """Up and Down follow what is drawn: top to bottom, and between the lines of wrapped buttons."""

    def form(self):
        return authform.Form([entry("east", "10.0.0.1", {"a": "1", "b": "2"})], "east", services=FakeServices())

    def test_down_from_every_action_button_reaches_the_save_row_whatever_the_width(self):
        for width in (160, 100, 70):
            for action in range(len(authform.ACTION_LABELS)):
                form = self.form()
                form.render(width, 40)                               # the buttons are placed on lines when drawn
                form.zone, form.action = "actions", action
                for _ in range(len(form.action_rows)):
                    form.press("down")
                    if form.zone != "actions":
                        break
                self.assertEqual(form.zone, "buttons", (width, action, form.action_rows))

    def test_up_from_the_save_row_lands_on_the_last_line_of_buttons_and_up_again_works_back_to_the_top(self):
        form = self.form()
        form.render(70, 40)
        self.assertGreater(len(form.action_rows), 1)                 # narrow: the buttons wrap onto several lines
        form.zone = "buttons"
        form.press("up")
        self.assertEqual(form.zone, "actions")
        self.assertIn(form.action, form.action_rows[-1])             # arriving from below lands on the last line of buttons
        for _ in range(len(form.action_rows) - 1):                   # up through the other lines ...
            form.press("up")
            self.assertEqual(form.zone, "actions")
        form.press("up")                                             # ... and on up to the accounts
        self.assertEqual(form.zone, "accounts")
        form.press("down")                                           # coming back down lands on the first line
        self.assertIn(form.action, form.action_rows[0])

    def test_down_inside_the_buttons_goes_to_the_next_line_first_keeping_its_place_along_the_line(self):
        form = self.form()
        form.render(70, 40)
        first = form.action_rows[0]
        form.zone, form.action = "actions", first[1]
        form.press("down")
        self.assertEqual(form.zone, "actions")
        self.assertIn(form.action, form.action_rows[1])
        self.assertEqual(form.action_rows[1].index(form.action), min(1, len(form.action_rows[1]) - 1))

    def test_the_walk_down_the_whole_screen_visits_each_part_once_in_the_order_drawn(self):
        form = self.form()
        form.render(160, 40)
        form.zone = "profile"
        order = [form.zone]
        for _ in range(10):
            form.press("down")
            if form.zone != order[-1]:
                order.append(form.zone)
        self.assertEqual(order[:6], ["profile", "address", "store", "accounts", "actions", "buttons"])

    def test_the_profile_in_use_row_is_drawn_above_the_accounts_and_so_is_reached_before_them(self):
        lines = [("".join(t for _s, t in l)) for l in self.form().render(120, 40)]
        at = lambda text: next(i for i, l in enumerate(lines) if text in l)
        self.assertLess(at("When none is named, use"), at("signs in with"))
        self.assertLess(at("signs in with"), at("[ Add account ]"))
        self.assertLess(at("[ Add account ]"), at("[ Save ]"))


class WorkingInAProfile(Home):
    """The profile in use decides where the list, settings, release table, records and backups live."""

    def test_a_command_with_a_profile_name_works_in_that_workspace_and_takes_its_address(self):
        lab = self.profile(address="192.0.2.5")
        args = cli.parse_args(["report", "--profile", "lab"])
        self.assertEqual(args._profile.name, "lab")
        self.assertEqual(args.source, "panorama:192.0.2.5")
        self.assertEqual(cli._ws(args, "upgradeList.txt"), lab.path("upgradeList.txt"))

    def test_naming_a_panorama_that_has_a_profile_uses_that_profile(self):
        self.profile(address="192.0.2.5")
        args = cli.parse_args(["report", "--panorama", "192.0.2.5"])
        self.assertEqual(args._profile.name, "lab")

    def test_a_panorama_with_no_profile_is_used_as_it_is_in_the_current_folder(self):
        self.profile(address="192.0.2.5")
        args = cli.parse_args(["report", "--panorama", "192.0.2.99"])
        self.assertIsNone(args._profile)
        self.assertEqual(cli._ws(args, "upgradeList.txt"), Path("upgradeList.txt"))

    def test_an_invented_fleet_never_uses_a_profile(self):
        self.profile()
        fleet = self.home / "fleet.json"
        fleet.write_text("{}", encoding="utf-8")
        self.assertIsNone(cli.parse_args(["report", "--source", f"fake:{fleet}"])._profile)

    def test_naming_no_panorama_and_having_no_profile_is_an_error_that_says_what_to_do(self):
        with mock.patch("sys.stderr", io.StringIO()) as err, self.assertRaises(SystemExit):
            cli.parse_args(["report"])
        self.assertIn("--panorama ADDRESS, or --profile NAME", err.getvalue())

    def test_an_unknown_profile_is_an_error_that_lists_the_ones_there_are(self):
        self.profile()
        with mock.patch("sys.stderr", io.StringIO()) as err, self.assertRaises(SystemExit):
            cli.parse_args(["report", "--profile", "nope"])
        self.assertIn("no profile called nope", err.getvalue())
        self.assertIn("lab", err.getvalue())

    def test_the_last_used_profile_is_used_when_no_panorama_is_named(self):
        self.profile("a", "10.0.0.1")
        self.profile("b", "10.0.0.2")
        profiles.remember("b")
        self.assertEqual(cli.parse_args(["report"])._profile.name, "b")

    def test_the_settings_list_and_release_table_in_the_workspace_are_found_and_pinned(self):
        lab = self.profile()
        lab.path("settings.toml").write_text("max_parallel = 2\n", encoding="utf-8")
        from panupgrade import releases
        releases.save(lab.path("releases.json"), releases.load())
        lab.path("upgradeList.txt").write_text("S1\nS2\n", encoding="utf-8")
        args = cli.parse_args(["plan", "--profile", "lab", "--target", "10.2.2"])
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli._load_settings(args).max_parallel, 2)
            cli._load_releases(args)
            self.assertEqual(cli._serials(args), ["S1", "S2"])
        text = out.getvalue()
        self.assertIn("found in the profile lab", text)
        self.assertEqual(Path(args.settings), lab.path("settings.toml").resolve())
        self.assertEqual(Path(args.releases), lab.path("releases.json").resolve())

    def test_backups_run_records_and_upgrade_kits_belong_with_the_profile(self):
        lab = self.profile()
        args = cli.parse_args(["plan", "--profile", "lab", "--target", "10.2.2"])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(Path(cli._load_settings(args).backup_dir), lab.path("backups"))
        prep = cli.parse_args(["prepare", "--profile", "lab", "--target", "10.2.2"])
        self.assertIsNone(prep.kit_dir)                                       # resolved to the workspace when it runs
        self.assertEqual(cli._ws(prep, "prepared"), lab.path("prepared"))
        other = cli.parse_args(["plan", "--panorama", "192.0.2.99", "--target", "10.2.2"])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli._load_settings(other).backup_dir, "backups")

    def test_the_upgrade_script_runs_in_the_same_workspace(self):
        from panupgrade import kit
        lab = self.profile(accounts={"a": "pw"})
        args = cli.parse_args(["prepare", "--profile", "lab", "--target", "10.2.2"])
        args._login = {"reading": "a", "changes": "a", "ask": None}
        argv = kit.upgrade_argv(args, "s.txt", "a.json", "j.jsonl")
        self.assertEqual(argv[argv.index("--profile") + 1], "lab")
        self.assertEqual(Path(argv[argv.index("--creds-file") + 1]), Path(lab.creds_file).resolve())


class WhenCredentialsAreMissing(Home):
    def args(self, **over):
        base = dict(source="panorama:192.0.2.1", creds_file=None, account=None, work_account=None, ask_credentials=None, color="never", ascii=True,
                    _profile=None)
        base.update(over)
        return argparse.Namespace(**base)

    def test_on_a_terminal_the_sign_in_screen_opens_and_the_command_carries_on_in_the_profile_it_made(self):
        def fix(args):
            lab = profiles.create("lab", "192.0.2.1")
            credstore.write(lab.creds_file, {"tool": "pw"})
            return True
        out = io.StringIO()
        args = self.args()
        with redirect_stdout(out), mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True), \
                mock.patch("panupgrade.cli._run_auth_screen", side_effect=fix) as screen_:
            login = cli._login(args)
        self.assertEqual(login["reading"], "tool")
        self.assertEqual(args._profile.name, "lab")
        screen_.assert_called_once()
        self.assertIn("Cannot sign in yet: there is no credentials file at", out.getvalue())

    def test_quitting_the_screen_without_saving_leaves_the_error(self):
        with redirect_stdout(io.StringIO()), mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True), \
                mock.patch("panupgrade.cli._run_auth_screen", return_value=False):
            with self.assertRaises(credstore.NeedsSignIn):
                cli._login(self.args())

    def test_a_script_gets_the_error_and_no_screen(self):
        with mock.patch("sys.stdin.isatty", return_value=False), mock.patch("panupgrade.cli._run_auth_screen") as screen_:
            with self.assertRaisesRegex(credstore.NeedsSignIn, "panupgrade auth"):
                cli._login(self.args())
        screen_.assert_not_called()

    def test_several_accounts_and_none_chosen_also_opens_the_screen_and_it_is_offered_only_once(self):
        self.creds("{'a': 'x', 'b': 'y'}")
        calls = []
        args = self.args(creds_file=str(self.home / "credentials.txt"))
        with redirect_stdout(io.StringIO()), mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True), \
                mock.patch("panupgrade.cli._run_auth_screen", side_effect=lambda a: calls.append(1) or False):
            for _ in range(2):
                with self.assertRaisesRegex(credstore.NeedsSignIn, "none is chosen"):
                    cli._login(args)
        self.assertEqual(len(calls), 1)

    def test_an_account_missing_from_the_file_is_offered_the_screen_too(self):
        self.creds("{'a': 'x'}")
        with redirect_stdout(io.StringIO()), mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True), \
                mock.patch("panupgrade.cli._run_auth_screen", return_value=False) as screen_:
            with self.assertRaises(credstore.NeedsSignIn):
                cli._key_for(self.args(account="ghost", creds_file=str(self.home / "credentials.txt")), "192.0.2.1", "reading")
        screen_.assert_called_once()

    def test_a_profile_supplies_the_account_to_read_with(self):
        lab = self.profile(accounts={"a": "x", "b": "y"}, account="b", work_account="a")
        login = cli._login(self.args(_profile=lab))
        self.assertEqual((login["reading"], login["changes"]), ("b", "a"))
        self.assertIn("from the profile lab", login["how"])


class TheAuthCommand(Home):
    def test_without_a_terminal_it_prints_the_profiles_and_their_accounts_with_no_secrets(self):
        self.profile("east", "10.0.0.1", {"person": SECRET, "service": {"api_key": "KEY-VALUE"}}, account="person")
        self.profile("west", "10.0.0.2")
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("sys.stdin.isatty", return_value=False):
            self.assertEqual(cli.main(["auth", "--color", "never", "--ascii"]), 0)
        text = out.getvalue()
        self.assertIn("east", text)
        self.assertIn("10.0.0.1", text)
        self.assertIn("account person: password (reads)", text)
        self.assertIn("account service: API key", text)
        self.assertIn("west", text)
        self.assertIn("no accounts yet", text)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("KEY-VALUE", text)

    def test_without_any_profile_it_says_how_to_make_one(self):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("sys.stdin.isatty", return_value=False):
            self.assertEqual(cli.main(["auth", "--color", "never", "--ascii"]), 0)
        self.assertIn("none yet", out.getvalue())

    def test_on_a_terminal_it_opens_the_screen_on_the_profile_for_the_panorama_named(self):
        self.profile("east", "10.0.0.1")
        self.profile("west", "10.0.0.2")
        seen = {}

        def fake_show(form, title=""):
            seen["active"], seen["modal"] = form.active, form.modal
            return False
        with redirect_stdout(io.StringIO()), mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True), \
                mock.patch("panupgrade.tui.show", side_effect=fake_show):
            code = cli.main(["auth", "--panorama", "10.0.0.2", "--color", "never", "--ascii"])
        self.assertEqual(code, 1)
        self.assertEqual((seen["active"], seen["modal"]), ("west", None))

    def test_a_panorama_with_no_profile_opens_the_new_profile_dialog_with_the_address_filled_in(self):
        seen = {}

        def fake_show(form, title=""):
            seen["modal"] = form.modal
            return False
        with redirect_stdout(io.StringIO()), mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True), \
                mock.patch("panupgrade.tui.show", side_effect=fake_show):
            cli.main(["auth", "--panorama", "192.0.2.1", "--color", "never", "--ascii"])
        self.assertEqual((seen["modal"]["type"], seen["modal"]["address"]), ("profile", "192.0.2.1"))

    def test_a_credentials_file_it_cannot_read_is_never_overwritten(self):
        lab = self.profile()
        lab_creds = Path(lab.creds_file)
        lab_creds.write_text("{'a': 5}", encoding="utf-8")
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True), \
                mock.patch("panupgrade.tui.show") as shown:
            cli.main(["auth", "--color", "never", "--ascii"])
        shown.assert_not_called()
        self.assertIn("will not overwrite", " ".join(out.getvalue().split()))
        self.assertEqual(lab_creds.read_text(encoding="utf-8"), "{'a': 5}")

    def test_the_screen_warns_about_variables_that_would_override_the_profile(self):
        self.profile()
        os.environ["PANUPGRADE_ACCOUNT"] = "x"
        form, _ = cli._auth_form(cli.parse_args(["auth"]))
        self.assertEqual(form.overrides, ["PANUPGRADE_ACCOUNT"])
        self.assertIn("PANUPGRADE_ACCOUNT", screen(form))


if __name__ == "__main__":
    unittest.main()
