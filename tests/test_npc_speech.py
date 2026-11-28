from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from catalog_db import Catalog, Writer
from catalog_dialogue import DialogueIndex
from catalog_models import ModelLibrary
from catalog_npc_speech import SEQUENCE_TABLES, STATIC_NPC, index_npc_speech
from catalog_sounds import SoundIndex
from catalog_sources import Files, SCRIPTS
from test_source_profiles import save


class NpcSpeechTests(unittest.TestCase):
    def test_comm_subtitles_keep_their_own_npc_and_recording(self):
        script = SCRIPTS / "fixture/sequence.java"
        original = SCRIPTS / "conversation/greeter.java"
        event = SCRIPTS / "conversation/captain.java"
        sequence = SEQUENCE_TABLES / "fixture_sequence.tab"
        files = Files({
            script.as_posix(): b"""
public int OnInitialize(obj_id self) {
    obj_id first = create.object("object/mobile/first.iff", where);
    attachScript(first, "conversation.greeter");
    sequencer.registerSequenceObject(first, "greeter");
    string_id firstLine = new string_id("fixture_comms", "together");
    npe.commTutorialPlayer(first, player, 3, firstLine, "sample/first.wav", "object/mobile/first.iff");
    obj_id second = create.object("object/mobile/second.iff", where);
    prose_package quote = prose.getPackage(new string_id("fixture_comms", "together"));
    commPlayers(second, "object/mobile/second.iff", "sample/second.wav", 3, player, quote);
    string_id departing = new string_id("fixture_comms", "farewell");
    npe.commTutorialPlayer(second, player, 3, departing, "sound/sys_comm_generic.snd", "object/mobile/second.iff");
    return 0;
}
public int Depart(obj_id self) {
    obj_id companion = sequencer.getSequenceObject(self, "greeter");
    string_id instruction = new string_id("fixture_comms", "launch");
    chat.chat(companion, player, instruction);
    play2dNonLoopingSound(player, "sample/launch.wav");
    return 0;
}""",
            original.as_posix(): b"""
public int Greet(obj_id self) {
    string_id message = new string_id("conversation/greeter", "s_1");
    play2dNonLoopingSound(player, "sample/hello.wav");
    npcSpeak(player, message);
    return 0;
}""",
            event.as_posix(): b"""
public int Greet(obj_id self) {
    string_id message = new string_id("conversation/captain", "s_1");
    play2dNonLoopingSound(player, "sample/captain.wav");
    play2dNonLoopingSound(player, "sample/mus_fan_fair_fade.wav");
    npcSpeak(player, message);
    return 0;
}""",
            (SCRIPTS / "content_tools/sequencer_master_object.java").as_posix(): b"""
public int action(String strAction, obj_id self) {
    switch (strAction.toLowerCase()) {
        case "secondcomm":
            npe.commTutorialPlayer(self, player, 3, message, cue, "object/mobile/second.iff");
            break;
    }
    return 0;
}""",
            sequence.as_posix(): (b"strActor\tstrAction\tstrData1\tstrData2\ns\ts\ts\ts\n"
                b"greeter\tsaySound\tstart\tsample/sequence.wav\n"
                b"greeter\tsecondComm\tconfirm\tsample/second_sequence.wav\n"),
            (STATIC_NPC / "captain.tpf").as_posix():
                b'objvars = [ "spawns" = "second", "quest_script" = "conversation.captain" ]\n',
        })
        text = {"fixture_comms": {"together": "We made it.", "farewell": "Until next time.", "launch": "Let's go."},
                "fixture_sequence": {"start": "The ship is ready.", "confirm": "The route is clear."},
                "conversation/greeter": {"s_1": "Hello."}, "conversation/captain": {"s_1": "Forward."}}
        strings = SimpleNamespace(conversation_names=lambda: list(text), prefetch=lambda _: None,
                                  table=lambda table: (text.get(table, {}), None))
        dialogue = DialogueIndex(strings, ())
        dialogue.scan(files)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("first", "second", "hello", "captain", "sequence", "second_sequence", "launch",
                         "mus_fan_fair_fade"):
                save(root / f"sample/{name}.wav", b"fixture audio")
            database = root / "catalog.sqlite3"
            writer = Writer(database)
            try:
                for npc in ("first", "second"):
                    writer.entity("npcs", npc, npc.title(), {"model_asset": f"object/mobile/{npc}.iff",
                                                            "scripts": [], "locations": []}, category="NPC")
                comms = index_npc_speech(writer, files, dialogue)
                sounds = SoundIndex(root, ModelLibrary(root))
                sounds.inventory()
                sounds.dialogue(writer, dialogue, comms)
                sounds.publish(writer)
                for table in dialogue.tables:
                    entry = dialogue.conversation(table)
                    for line in entry["lines"]:
                        if cues := sounds.line_audio.get((table, line["key"])):
                            line["sounds"] = cues
                    writer.entity("conversations", table, entry["name"], entry)
                writer.finish({"revision": "fixture", "locations": {}, "maps": {}})
            finally:
                writer.connection.close()

            catalog = Catalog(database)
            first = catalog.dialogue("fixture_comms", "", "", 1, 20, npc="first")["rows"]
            second = catalog.dialogue("fixture_comms", "", "", 1, 20, npc="second")["rows"]
            self.assertEqual([line["key"] for line in first], ["together", "launch"])
            self.assertEqual([cue["clip"] for cue in first[1]["sounds"]], ["sample/launch.wav"])
            self.assertEqual([line["key"] for line in second], ["together", "farewell"])
            self.assertEqual([cue["clip"] for cue in first[0]["sounds"]], ["sample/first.wav"])
            self.assertEqual([cue["clip"] for cue in second[0]["sounds"]], ["sample/second.wav"])
            self.assertFalse(second[1].get("sounds"))
            for npc, expected in (("first", {"sample/first.wav", "sample/hello.wav", "sample/sequence.wav",
                                             "sample/launch.wav"}),
                                  ("second", {"sample/second.wav", "sample/captain.wav", "sample/second_sequence.wav"})):
                rows = catalog.links("npcs", npc, "sounds", 1, 20)["rows"]
                voice = [row for row in rows if row["detail"]["reason"] == "Recorded dialogue"]
                self.assertEqual({row["clip"] for row in voice}, expected)
                self.assertTrue(all(row["detail"]["dialogue_lines"][0]["text"] for row in voice))
            music = rows[-1]
            self.assertEqual(music["clip"], "sample/mus_fan_fair_fade.wav")
            self.assertEqual(music["detail"]["reason"], "Music cue")
            captain, = catalog.dialogue("conversation/captain", "", "", 1, 20, npc="second")["rows"]
            self.assertEqual([cue["id"] for cue in captain["sounds"] if cue.get("music")], [music["id"]])


if __name__ == "__main__":
    unittest.main()
