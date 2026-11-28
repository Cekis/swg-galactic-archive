"""Recover NPC recordings from authored greeting, encounter, and collection tables."""

from collections import defaultdict
from pathlib import PurePosixPath
import re

from catalog_sounds import _music_cue, sound_id
from catalog_sources import SERVER, SHARED, string_ref


GREETER_PROFILES = SERVER / "vendor/greeter_nonvendor_data.tab"
GREETER_AUDIO = SERVER / "vendor/greeter_sound_voice_effect.tab"
MOBS = SERVER / "mob/creatures.tab"
HEROIC_TABLES = SERVER / "spawning/heroic"
COLLECTIONS = SHARED / "collection/collection.tab"
QUEST_TASKS = SHARED / "questtask"

GREETER_OVERRIDE = re.compile(r"\bstring:(greeter\.sound_vo(?:_string)?)=(\w+)")
WORDS = re.compile(r"[a-z0-9]+")


def greeter_voices(files, resolve):
    """Read the NPC-specific column overrides used by terminal.greeter."""
    options = files.tab(GREETER_AUDIO)
    if not options:
        return
    overrides = {}
    for mob in files.tab(MOBS):
        fields = dict(GREETER_OVERRIDE.findall(mob.get("objvars")))
        if "greeter.sound_vo" in fields and "greeter.sound_vo_string" in fields:
            overrides[mob.get("creatureName")] = (fields["greeter.sound_vo"], fields["greeter.sound_vo_string"], mob.source)
    columns = options[0].cells
    for profile in files.tab(GREETER_PROFILES):
        voiced = profile.get("greeter_voice_over") == "1"
        effects = profile.get("greeter_sounds") == "1"
        if not voiced and not (effects and profile.get("greeter_speaks_basic") == "1"):
            continue
        stem = "greeter_" + profile.get("creature_type")
        default_column = stem + ("_voice" if voiced else "_sound")
        if default_column not in columns and effects:
            default_column = stem + "_sound"
        for actor in profile.get("greeter_creature_name").strip('"').split(","):
            actor = actor.strip()
            if not (npc := resolve(actor)):
                continue
            column, captions, proof = overrides.get(actor, (default_column, default_column + "_string", profile.source))
            for row in options:
                audio = sound_id(row.get(column).strip('"'))
                ref = string_ref("@player_vendor:" + row.get(captions).strip('"'))
                if audio and ref:
                    yield npc, ref, audio, row.source, "greeter voice option", proof, "Greeter menu caption"


def _spoken_phrase(audio, text):
    # When a trigger shows several messages, a literal phrase in the cue name
    # can identify the spoken one without assuming every message is voiced.
    words = WORDS.findall(PurePosixPath(audio).stem.casefold())
    if len(words) < 3:
        return False
    phrase = " " + " ".join(words[-3:]) + " "
    return phrase in (" " + " ".join(WORDS.findall(text.casefold())) + " ")


def _names_actor(audio, scene, npc, role):
    name = PurePosixPath(audio).stem.casefold()
    prefix = scene.casefold() + "_"
    character = name.removeprefix(prefix).split("_")[0]
    return name.startswith(prefix) and character in WORDS.findall(f"{npc}_{role}".casefold())


def _encounter_portraits(messages, spawns):
    """Identify comm portraits used for the same line as a named stage actor."""
    portraits = defaultdict(set)
    for options in messages.values():
        for entries in options.values():
            cast = {(npc, role) for onstage, role, _ in entries if onstage for npc in spawns[role]}
            if len(cast) == 1:
                for onstage, portrait, _ in entries:
                    if not onstage:
                        portraits[portrait].update(cast)
    return portraits


def encounter_voices(files, resolve, strings):
    """Match localized comms or theater chat with the audio in the same trigger."""
    table = "sequencer_spam"
    client_lines = strings.table(table)[0]
    for path in files.paths(HEROIC_TABLES, "*.tab", recursive=True):
        actors = set()
        spawns = defaultdict(set)
        messages = defaultdict(lambda: defaultdict(list))
        sounds = defaultdict(dict)
        for row in files.tab(path):
            parts = row.get("object").strip('"').split(":")
            trigger = row.get("triggerId")
            if parts[0] == "messagePlayers" and trigger and len(parts) >= 3:
                if ref := string_ref(f"@{table}:{parts[1]}"):
                    messages[trigger][ref].append((False, parts[2], row.source))
            elif parts[0] == "doTheaterAction" and trigger and len(parts) >= 5 and parts[2] == "chat":
                if ref := string_ref(f"@{table}:{parts[4]}"):
                    messages[trigger][ref].append((True, parts[1], row.source))
            elif parts[0] == "fxOnPlayers" and trigger and len(parts) >= 2:
                if audio := sound_id(parts[1]):
                    sounds[trigger].setdefault(audio, row.source)
            elif len(parts) == 1 and (npc := resolve(parts[0])):
                actors.add(npc)
                if spawn_id := row.get("spawn_id"):
                    spawns[spawn_id].add(npc)

        portraits = _encounter_portraits(messages, spawns)
        for trigger, options in messages.items():
            cues = sounds.get(trigger, {})
            if len(cues) != 1:
                continue
            audio, proof = next(iter(cues.items()))
            refs = list(options)
            if len(refs) > 1:
                refs = [ref for ref in refs if _spoken_phrase(audio, client_lines.get(ref[1], ""))]
            if len(refs) != 1:
                continue
            ref = refs[0]
            theater, comms = {}, {}
            phrase_matches = _spoken_phrase(audio, client_lines.get(ref[1], ""))
            for onstage, actor, binding in options[ref]:
                if onstage:
                    for npc in spawns[actor]:
                        if phrase_matches or _names_actor(audio, path.stem, npc, actor):
                            theater[npc] = binding
                    continue
                named = {npc for npc, role in portraits[actor] if _names_actor(audio, path.stem, npc, role)}
                npc = next(iter(named)) if len(named) == 1 else resolve(actor, actors)
                if npc:
                    comms[npc] = binding
            speakers = theater or comms
            if len(speakers) == 1:
                npc, binding = next(iter(speakers.items()))
                yield npc, ref, audio, proof, "encounter dialogue", binding


def collection_voices(files, resolve):
    """Read explicit speaker, subtitle, and voice fields from Saga comm slots."""
    for row in files.tab(COLLECTIONS):
        fields = dict(value.split(":", 1) for key, value in row.cells.items()
                      if key.startswith("category") and ":" in value)
        if fields.get("UIType") != "Message":
            continue
        npc = resolve(fields.get("appearance"))
        ref = string_ref(fields.get("message", ""))
        audio = sound_id(fields.get("voice_over"))
        if npc and ref and audio:
            note = "Development collection entry" if "test" in row.get("slotName").lower().split("_") else None
            yield npc, ref, audio, row.source, "collection comm", None, note


def quest_voices(files, resolve, quests, givers):
    """Read the separate audio and text fields used by quest task handlers."""
    for path in files.paths(QUEST_TASKS, "*.tab", recursive=True):
        quest = path.relative_to(QUEST_TASKS).with_suffix("").as_posix()
        if quest not in quests:
            continue
        for row in files.tab(path):
            script = row.get("ATTACH_SCRIPT")
            if script == "quest.task.ground.comm_player":
                audio = sound_id(row.get("MUSIC_ON_ACTIVATE"))
                ref = string_ref(row.get("COMM_MESSAGE_TEXT"))
                npc = resolve(row.get("NPC_APPEARANCE_SERVER_TEMPLATE"), givers.get(quest))
                role, note = "quest comm", None
            elif script == "quest.task.ground.show_message_box":
                audio = sound_id(row.get("MESSAGE_BOX_SOUND"))
                ref = string_ref(row.get("MESSAGE_BOX_TEXT"))
                npc = None
                role, note = "quest message audio", "Quest message audio"
            else:
                continue
            if audio and ref and not _music_cue(audio):
                yield quest, npc, ref, audio, row.source, role, note
