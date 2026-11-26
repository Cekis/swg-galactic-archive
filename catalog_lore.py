"""Describe catalog locations using game text, source records and cited lore."""

from collections import defaultdict
from math import hypot
import re
import struct

from catalog_sources import display_id, read_iff_table


POI_SOURCE = "datatables/clientpoi/clientpoi.iff"
DATABANK = "https://www.starwars.com/databank/"
GALAXIES_WIKI = "https://swglegends.com/wiki/index.php?title="
WORLD_LORE = {
    "corellia": ("Corellia is a major Core World whose shipyards and pilots helped shape galactic travel. "
                 "The Empire put its industrial capacity to work producing TIE fighters and Star Destroyers. "
                 "In Star Wars Galaxies, Coronet is one of the world's principal ports.", DATABANK + "corellia"),
    "dantooine": ("Forests and open country cover this remote Outer Rim world. The Rebel Alliance established an early "
                  "headquarters on Dantooine, then withdrew to keep the Empire from finding it. The abandoned base "
                  "is one of the landmarks recorded in the game.", DATABANK + "dantooine"),
    "dathomir": ("Under Dathomir's red light, tangled forests give way to swamps and the stone strongholds of the "
                 "Nightsisters. The witches drew on the Force through their own traditions, while the Nightbrothers "
                 "lived apart. The game also places rancor lairs and clan settlements across its wilderness.",
                 DATABANK + "dathomir"),
    "endor": ("Endor's forest moon is the home of the Ewoks. Its woods concealed the Imperial shield installation "
              "for the second Death Star, and Ewok help proved decisive when the Rebel Alliance attacked it.",
              DATABANK + "endor"),
    "hoth": ("An icy world on the Outer Rim, Hoth offered the Rebel Alliance cover for Echo Base. "
             "Tauntauns could travel across its frozen plains, while wampas made the wilderness dangerous. "
             "The Empire discovered the base and forced the Rebels to evacuate.", DATABANK + "hoth"),
    "kashyyyk": ("Kashyyyk is the Wookiee homeworld, where communities built around massive trees use highly developed "
                 "technology. The Republic and Separatists fought for the wooded Mid Rim world late in the Clone Wars. "
                 "The game charts several distinct areas, including Kachirho and the Rryatt Trail.", DATABANK + "kashyyyk"),
    "lok": ("Lok is an arid world in the Karthakk system, where sulfur pools and the active Mount Chaolt break up the "
            "wasteland. The Feeorin pirate Nym controls the main port at his stronghold. His operation shares the planet "
            "with dangerous wildlife, including the enormous kimogila.", GALAXIES_WIKI + "Lok"),
    "mustafar": ("Mustafar is an Outer Rim volcanic world with valuable mineral deposits, long worked by the Techno "
                 "Union. Lava fields and rivers shape its landscape. Obi-Wan Kenobi and Anakin Skywalker fought their "
                 "decisive duel here near the end of the Clone Wars.", DATABANK + "mustafar"),
    "naboo": ("Naboo is a temperate world of broad plains, lakes and swamps. The Naboo build river cities such as Theed. "
              "The native Gungans are known for their underwater settlements. The two peoples allied to resist the Trade "
              "Federation's occupation.", DATABANK + "naboo"),
    "ord_mantell": ("Ord Mantell is a mountainous planet that once harbored Black Sun operations. Fighting during the "
                    "Clone Wars and hard times under the Empire left work for people willing to operate outside the law. "
                    "The game's Ord Mantell entries include several routes through the surrounding sector.",
                    DATABANK + "ord-mantell"),
    "rori": ("Rori is a wooded, swampy moon of Naboo. Its largest city, Narmle, takes its name from the Naboo king who "
             "sponsored the settlement. Restuss became a notable front in the Galactic Civil War, and the moon's wetlands "
             "also shelter Gungan settlements.", GALAXIES_WIKI + "Rori"),
    "talus": ("Talus and its sister world Tralus share an orbit around a common center in the Corellian system. Talus is "
              "more sparsely settled than Corellia. Dearic and Nashal are its main towns in the game, alongside an Imperial "
              "outpost and sites associated with the Binayre pirates.", GALAXIES_WIKI + "Talus"),
    "tatooine": ("Tatooine is an Outer Rim desert world with two suns. Moisture farms provide a livelihood far from "
                "the ports of Mos Eisley and Mos Espa, where smugglers and traders come and go under Hutt influence. "
                "Both Anakin and Luke Skywalker spent their early lives here.", DATABANK + "tatooine"),
    "yavin4": ("Yavin IV is a jungle moon of the gas giant Yavin. Its isolation gave the Rebel Alliance room to build a "
              "base from which it eventually launched the attack against the first Death Star. Ancient ruins and dense "
              "forest shape the locations marked on the game map.", DATABANK + "yavin-4"),
    "space_light1": ("This space zone covers the Kessel system. Kessel is best known for its spice mines and the smuggling "
                     "routes that serve them. The surrounding routes gave the Kessel Run its reputation as a test of "
                     "navigation and nerve.", DATABANK + "kessel"),
}
WORLD_ALIASES = {"adventure2": "hoth", "kashyyyk_main": "kashyyyk"}
SPACE_WORLDS = {"space_lok": "lok", "space_nova_orion": "ord_mantell", "space_endor": "endor"}
LOCAL_LORE = {
    ("tatooine", "mos eisley"): ("Mos Eisley is a desert spaceport where traders and smugglers cross paths. Pilots gather "
                                "at its cantina, where travelers can find crews and off-world passage.",
                                DATABANK + "mos-eisley-cantina"),
    ("tatooine", "mos espa"): ("Mos Espa's domed buildings shelter one of Tatooine's main ports. The Hutts once held power "
                              "here, and the nearby Grand Arena drew crowds for the Boonta Eve podrace.",
                              DATABANK + "mos-espa"),
    ("tatooine", "lars homestead"): ("Owen and Beru Lars raised Luke Skywalker at this moisture farm. "
                                    "The family made a living harvesting water from the air in Tatooine's desert.",
                                    DATABANK + "lars-moisture-farm"),
    ("tatooine", "jabba s palace"): ("Jabba the Hutt ran his criminal operation from a fortified palace in the Tatooine "
                                    "desert. Its throne room was a gathering place for his court, guards and bounty hunters.",
                                    DATABANK + "jabba-the-hutts-palace"),
    ("naboo", "theed"): ("Theed is Naboo's royal capital. Its palace served as the seat of government and became the "
                        "focus of the fight to retake the city from the Trade Federation.",
                        DATABANK + "theed-royal-palace"),
    ("naboo", "otoh gunga"): ("Otoh Gunga is a Gungan city beneath Naboo's waters. It is built from interconnected "
                             "enclosures that keep the water out while letting its inhabitants see into the lake.",
                             DATABANK + "otoh-gunga"),
    ("adventure2", "echo base"): ("The Rebel Alliance expanded Hoth's natural caverns into Echo Base. An ion cannon "
                                 "protected the evacuation when Imperial forces discovered the base.",
                                 DATABANK + "echo-base"),
}


def _slug(value):
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def load_client_landmarks(clientdata, strings, spatial, places, world):
    """Use the same names, descriptions and coordinates as the game's POI catalog."""
    try:
        _, _, rows = read_iff_table(clientdata / POI_SOURCE)
    except (OSError, AssertionError, ValueError, struct.error):
        return 0
    strings.prefetch(("clientpoi_n", "clientpoi_d"))
    count = 0
    for ordinal, row in enumerate(rows, 1):
        scene = row.get("Planet")
        if not scene or not re.fullmatch(r"[a-z][a-z0-9_]*", scene) or not row.get("Name"):
            continue
        world(scene)
        name = strings.lookup(row["Name"])
        description = strings.lookup(row.get("Description", ""))
        ident = f"{scene}/poi/{name.get('key') or ordinal}"
        if ident in places:
            ident += "-" + str(ordinal)
        coords = [row["X"], row["Y"], row["Z"]]
        places[ident] = {
            "id": ident, "name": name.get("text") or display_id(name.get("key") or row["Name"]),
            "scene": scene, "category": "Landmark", "name_label": name, "client_description": description,
            "marker": {"coordinates": coords, "status": "authored_world"},
            "map": spatial.maps.get(spatial.map_id_for(scene, coords)),
            "sources": [{"path": "clientdata/" + POI_SOURCE, "row": ordinal}],
        }
        count += 1
    return count


def _position(place):
    return (place.get("marker") or {}).get("coordinates")


def _nearby(place, places):
    if not (coords := _position(place)):
        return None
    x, _, z = coords
    options = ((hypot(x - point[0], z - point[2]), other)
               for other, point in places.get(place["scene"], ())
               if other["id"] != place["id"] and _slug(other["name"]) != _slug(place["name"]))
    distance, name = min(options, default=(float("inf"), None), key=lambda item: item[0])
    return (distance, name) if distance <= 1250 else None


def _source_page(url):
    return {"url": url, "label": "Star Wars Databank" if url.startswith(DATABANK) else "Star Wars Galaxies lore reference"}


def _game_text(label):
    return " ".join((label or {}).get("text", "").split()) if (label or {}).get("text") else ""


def _local_description(place, scene, nearby):
    name, category = place["name"], place["category"]
    if category == "Transport":
        hint = "shuttle stop" if place.get("transport_type") == "shuttleport" else "starport"
        intro = f"{name} is a registered {hint} on {scene}."
    elif category == "Buildout area":
        intro = f"{name} is a mapped sector of {scene}."
        if event := place.get("required_event"):
            intro += f" Its placements are associated with the {display_id(event)} event."
    elif category == "Landmark":
        intro = f"{name} is a named point of interest on {scene}."
    else:
        intro = f"{name} is a named region of {scene}."
    if bounds := place.get("bounds"):
        width, height = round(bounds[2] - bounds[0]), round(bounds[3] - bounds[1])
        if width > 0 and height > 0:
            intro += f" Its mapped footprint is {width:,} by {height:,} meters."
    elif (boundary := place.get("boundary")) and (values := boundary.get("values")):
        if boundary.get("shape") == "circle":
            intro += f" The region extends {round(values[2]):,} meters from its center."
        elif (width := round(values[2] - values[0])) > 0 and (height := round(values[3] - values[1])) > 0:
            intro += f" Its mapped footprint is {width:,} by {height:,} meters."
    if point := _position(place):
        descriptor = "Its marked center" if category in ("Buildout area", "Region") else "Its map marker"
        intro += f" {descriptor} is at X {round(point[0]):,}, Z {round(point[2]):,}."
    if nearby:
        distance, reference = nearby
        intro += (f" It shares the vicinity of {reference['name']}." if distance < 25
                  else f" {reference['name']} is about {round(distance / 10) * 10:,} meters away.")
    return intro


def add_location_descriptions(places, strings):
    named = defaultdict(list)
    client_text = defaultdict(dict)
    for place in places.values():
        if (coords := _position(place)) and place["category"] in ("Transport", "Landmark"):
            named[place["scene"]].append((place, coords))
        if _game_text(place.get("client_description")):
            client_text[place["scene"]].setdefault(_slug(place["name"]), (coords, place["client_description"]))

    counts = defaultdict(int)
    for ident, place in places.items():
        scene, name = place["scene"], place["name"]
        world = places.get(scene, place)
        canonical = WORLD_ALIASES.get(scene, scene)
        record = place.get("client_description")
        if not _game_text(record) and (candidate := client_text[scene].get(_slug(name))):
            actual = _position(place)
            if actual and candidate[0] and hypot(actual[0] - candidate[0][0], actual[2] - candidate[0][2]) < 1250:
                record = candidate[1]
        curated = WORLD_LORE.get(canonical) if scene == ident else LOCAL_LORE.get((scene, _slug(name)))
        sources = list(place["sources"][:2])
        if original := _game_text(record):
            text = original
            if record.get("asset"):
                sources.append({"path": record["asset"], "key": record.get("code")})
            counts["client_text"] += 1
        elif curated:
            text, citation = curated
            sources.append(_source_page(citation))
            counts["lore_reference"] += 1
        elif scene != ident:
            near = _nearby(place, named)
            text = _local_description(place, world["name"], near)
            if near:
                sources.extend(near[1]["sources"][:1])
            counts["location_records"] += 1
        elif scene == "none":
            text = "The game uses this identifier for records without an assigned world. Attached records provide their scene when one is available."
            counts["location_records"] += 1
        else:
            chart = place.get("map") or {}
            if scene.startswith("space_"):
                target = SPACE_WORLDS.get(scene, scene.removeprefix("space_"))
                context = places.get(target)
                text = f"{name} is a charted space sector" + (f" around {context['name']}." if context else ".")
                if chart.get("layers", {}).get("transit"):
                    text += " Its chart includes designated hyperspace arrivals."
                if chart.get("layers", {}).get("resource"):
                    text += " The asteroid field records identify some of the sites that pilots can navigate by."
                if context and target in WORLD_LORE:
                    sources.append(_source_page(WORLD_LORE[target][1]))
                    place["lore_parent"] = target
            else:
                text = f"{name} is a distinct scene in Star Wars Galaxies."
                if chart.get("layers"):
                    text += " The map records the known buildout and quest placements for this scene."
                if scene.startswith("kashyyyk_"):
                    text += " It is part of the Wookiee homeworld, where settlements sit amid dense forests."
                    sources.append(_source_page(WORLD_LORE["kashyyyk"][1]))
                    place["lore_parent"] = "kashyyyk"
                elif scene == "tutorial":
                    text += " The game identifies it as an introductory area aboard a space station."
            if not sources and (proof := chart.get("extent_source")):
                sources.append(proof)
            counts["location_records"] += 1
        if ident != scene:
            place.setdefault("lore_parent", scene)
        elif canonical != scene:
            place.setdefault("lore_parent", canonical)
        if not sources and scene == ident:
            for table in ("planet_n", "zone_n"):
                label = strings.lookup(f"@{table}:{scene}")
                if label.get("text") and label.get("asset"):
                    sources.append({"path": label["asset"], "key": label.get("code")})
                    break
        place["description"] = {"text": text, "sources": sources}
    return dict(counts)
