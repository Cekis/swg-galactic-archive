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
from catalog_voice_tables import COLLECTIONS, GREETER_AUDIO, GREETER_PROFILES, HEROIC_TABLES, MOBS, QUEST_TASKS
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


    def test_authored_voice_tables_preserve_npc_variants_and_source_captions(self):
        encounter = HEROIC_TABLES / "fixture.tab"
        sources = Files({
            GREETER_PROFILES.as_posix(): (
                b"creature_type\tgreeter_creature_name\tgreeter_voice_over\tgreeter_sounds\tgreeter_speaks_basic\n"
                b's\ts\ti\ti\ti\ngreeter_fixture\t"first,second"\t1\nspoken_effect\trobot\t0\t1\t1\n'),
            GREETER_AUDIO.as_posix(): (
                b"greeter_greeter_fixture_voice\tgreeter_greeter_fixture_voice_string\tother_voice\tother_caption\t"
                b"greeter_spoken_effect_sound\tgreeter_spoken_effect_sound_string\n"
                b"s\ts\ts\ts\ts\ts\nsample/default.wav\tdefault_caption\tsample/alternate.wav\talternate_caption\t"
                b"sample/robot.wav\trobot_caption\n"),
            MOBS.as_posix(): (
                b"creatureName\tobjvars\ns\ts\nsecond\t"
                b"string:greeter.sound_vo=other_voice,string:greeter.sound_vo_string=other_caption\n"),
            encounter.as_posix(): (
                b"object\ttriggerId\tspawn_id\ns\ts\ts\nspeaker\tspawn_trigger\tstage_speaker\n"
                b"messagePlayers:report:object/mobile/general.iff:3.0\treport_trigger\n"
                b"fxOnPlayers:sample/spotted_imperial_walkers.wav\treport_trigger\n"
                b"messagePlayers:reply:object/mobile/second.iff:5.0\treport_trigger\n"
                b"messagePlayers:report:object/mobile/general.iff:3.0\tambiguous\n"
                b"messagePlayers:reply:object/mobile/second.iff:5.0\tambiguous\n"
                b"fxOnPlayers:sample/unrelated.wav\tambiguous\n"
                b"doTheaterAction:stage_speaker:chat:say:stage_line\ttheater\n"
                b"messagePlayers:stage_line:object/mobile/portrait.iff:3.0\ttheater\n"
                b"fxOnPlayers:sample/fixture_speaker_proceed.wav:50\ttheater\n"
                b"messagePlayers:encore:object/mobile/portrait.iff:3.0\tencore\n"
                b"fxOnPlayers:sample/fixture_speaker_encore.wav\tencore\n"),
            (QUEST_TASKS / "quest/fixture.tab").as_posix(): (
                b"ATTACH_SCRIPT\tMUSIC_ON_ACTIVATE\tCOMM_MESSAGE_TEXT\tNPC_APPEARANCE_SERVER_TEMPLATE\t"
                b"MESSAGE_BOX_TEXT\tMESSAGE_BOX_SOUND\ns\ts\ts\ts\ts\ts\n"
                b"quest.task.ground.comm_player\tsample/quest_voice.wav\t@quest/ground/fixture:comm\t"
                b"object/mobile/general.iff\n"
                b"quest.task.ground.show_message_box\t\t\t\t@quest/ground/fixture:message\t"
                b"sample/tutorial_voice.wav\n"
                b"quest.task.ground.comm_player\tsample/mus_underscore.wav\t@quest/ground/fixture:other\t"
                b"object/mobile/general.iff\n"),
            COLLECTIONS.as_posix(): (
                b"slotName\tcategory1\tcategory2\tcategory3\tcategory4\ns\ts\ts\ts\ts\n"
                b"fixture_test_message\tUIType:Message\tappearance:object/mobile/first.iff\t"
                b"message:saga_relic:hello\tvoice_over:sample/collection.wav\n"),
        })
        lines = {"player_vendor": {"default_caption": "Default menu choice", "alternate_caption": "Alternate menu choice",
                                   "robot_caption": "Check your weapons please."},
                 "sequencer_spam": {"report": "We spotted Imperial walkers!", "reply": "We'll send reinforcements.",
                                    "stage_line": "You may continue.", "encore": "Good work."},
                 "saga_relic": {"hello": "Testing this transmission."},
                 "quest/ground/fixture": {"comm": "Come see me.", "message": "Follow the tutorial arrow.",
                                          "other": "An unrelated comm."}}
        strings = SimpleNamespace(conversation_names=lambda: list(lines),
                                  table=lambda name: (lines.get(name, {}), None))
        dialogue = DialogueIndex(strings, ())
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for clip in ("default", "alternate", "robot", "spotted_imperial_walkers", "fixture_speaker_proceed",
                         "fixture_speaker_encore", "unrelated", "collection", "quest_voice", "tutorial_voice"):
                save(root / f"sample/{clip}.wav", b"fixture audio")
            writer = Writer(root / "catalog.sqlite3")
            try:
                writer.entity("quests", "quest/fixture", "Tutorial", {})
                writer.link("quests", "quest/fixture", "quest_givers", "npcs", "speaker")
                models = {"first": "first", "second": "second", "robot": "robot",
                          "speaker": "general", "general": "general", "portrait": "portrait"}
                for npc, model in models.items():
                    writer.entity("npcs", npc, npc.title(), {"model_asset": f"object/mobile/{model}.iff",
                                                            "locations": []}, category="NPC")
                sounds = SoundIndex(root, ModelLibrary(root))
                sounds.inventory()
                sounds.dialogue(writer, dialogue, index_npc_speech(writer, sources, dialogue))
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

            catalog = Catalog(root / "catalog.sqlite3")
            for npc, key, clip in (("first", "default_caption", "default"), ("second", "alternate_caption", "alternate"),
                                   ("robot", "robot_caption", "robot")):
                line, = catalog.dialogue("player_vendor", "", "", 1, 20, npc=npc)["rows"]
                cue, = line["sounds"]
                self.assertEqual((line["code"], cue["clip"], cue["caption_note"]),
                    (f"@player_vendor:{key}", f"sample/{clip}.wav", "Greeter menu caption"))
            rows = catalog.links("npcs", "speaker", "sounds", 1, 20)["rows"]
            self.assertEqual({row["clip"] for row in rows},
                {"sample/spotted_imperial_walkers.wav", "sample/fixture_speaker_proceed.wav",
                 "sample/fixture_speaker_encore.wav", "sample/quest_voice.wav"})
            row = next(row for row in rows if row["clip"] == "sample/spotted_imperial_walkers.wav")
            subtitle, = row["detail"]["dialogue_lines"]
            self.assertEqual(subtitle["code"], "@sequencer_spam:report")
            self.assertEqual(subtitle["text"], "We spotted Imperial walkers!")
            self.assertEqual(catalog.links("npcs", "general", "sounds", 1, 20)["total"], 0)
            self.assertEqual(catalog.links("npcs", "portrait", "sounds", 1, 20)["total"], 0)
            collection, = catalog.dialogue("saga_relic", "", "", 1, 20, npc="first")["rows"]
            self.assertEqual(collection["sounds"][0]["caption_note"], "Development collection entry")
            quest = catalog.entity("quests", "quest/fixture")
            self.assertEqual({row["clip"] for row in quest["related"]["sounds"]["rows"]},
                {"sample/quest_voice.wav", "sample/tutorial_voice.wav"})
            comm, = catalog.dialogue("quest/ground/fixture", "", "", 1, 20, npc="speaker")["rows"]
            self.assertEqual(comm["sounds"][0]["clip"], "sample/quest_voice.wav")
            message, = catalog.dialogue("quest/ground/fixture", "@quest/ground/fixture:message", "", 1, 20)["rows"]
            self.assertEqual(message["sounds"][0]["caption_note"], "Quest message audio")
            self.assertNotIn("npc", message["sounds"][0])


if __name__ == "__main__":
    unittest.main()
