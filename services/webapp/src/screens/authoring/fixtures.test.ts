import {
  actionOf,
  clueOf,
  draftOf,
  encounterOf,
  exitOf,
  findingOf,
  monsterGroupOf,
  monsterOf,
  reportOf,
  revelationOf,
  srdMonsterOf,
  featureOf,
  itemOf,
  locationOf,
  npcOf,
  refsOf,
  relationOf,
  secretOf,
} from "./fixtures";

// Заглушки отвечают как сервер, только пока их формы совпадают с контрактом: поля сущностей — поля
// моделей движка (`dnd_engine.adventure.schema`), которые сервер отдаёт `model_dump(mode="json")`
// целиком, вместе с полями по умолчанию. Если поле потеряется в заглушке, тесты экранов перестанут
// ловить его потерю при сохранении.
const keys = (value: object) => Object.keys(value).sort();
const JOURNAL = ["public", "known_at_start"];

describe("заглушки совпадают с формами контракта", () => {
  test("черновик — все поля ответа сервера", () => {
    expect(keys(draftOf())).toEqual(
      [
        "id",
        "version",
        "title",
        "adventure_id",
        "journal_intro",
        "settings",
        "world",
        "deities",
        "factions",
        "plot",
        "locations",
        "npcs",
        "relations",
        "encounters",
        "monsters",
        "items",
        "revelations",
        "start_location",
        "finale_location",
        "locks",
        "wishes",
        "steps",
        "job",
      ].sort(),
    );
  });

  test("локация — Location движка с выходами, деталями, секретами, зонами и связями зон", () => {
    const location = locationOf();

    expect(keys(location)).toEqual(
      [
        ...JOURNAL,
        "id",
        "name",
        "region",
        "read_aloud",
        "dm_notes",
        "exits",
        "features",
        "secrets",
        "encounter_id",
        "zones",
        "links",
      ].sort(),
    );
    expect(location.zones).toEqual([{ id: "here", name: "здесь" }]);
    expect(location.links).toEqual([]);
    expect(location.encounter_id).toBeNull();
  });

  test("выход, деталь и секрет локации — Exit, Feature, LocationSecret", () => {
    expect(keys(exitOf())).toEqual(
      ["id", "to", "direction", "description", "locked_by", "hidden_dc"].sort(),
    );
    expect(keys(featureOf())).toEqual(
      [
        "id",
        "name",
        "description",
        "check",
        "reveals",
        "hidden",
        "locked_by",
      ].sort(),
    );
    expect(keys(secretOf())).toEqual(["id", "text", "check", "reveals"].sort());
  });

  test("NPC — Npc движка; связи формата v1 в конструкторе пусты", () => {
    const npc = npcOf();

    expect(keys(npc)).toEqual(
      [
        ...JOURNAL,
        "id",
        "name",
        "role",
        "appearance",
        "manner",
        "motive",
        "secret",
        "knows",
        "attitude",
        "relations",
        "location_id",
        "statblock",
      ].sort(),
    );
    expect(npc.relations).toEqual([]);
  });

  test("связь — Relation, предмет — Item с держателем", () => {
    expect(keys(relationOf())).toEqual(
      ["id", "a", "b", "kind", "note", "secret", "mutual"].sort(),
    );
    expect(keys(itemOf())).toEqual(
      [...JOURNAL, "id", "name", "description", "base_item", "holder"].sort(),
    );
    expect(itemOf().holder).toBe("location:crypt");
  });

  test("справочник ссылок — {ref, kind, name}: виды по порядку, имя или id", () => {
    const draft = draftOf({
      locations: [locationOf({ id: "crypt", name: "" })],
      npcs: [npcOf()],
      items: [itemOf()],
      encounters: [encounterOf({ id: "crypt-ghouls" })],
    });

    expect(refsOf(draft)).toEqual([
      { ref: "npc:marta", kind: "npc", name: "Марта" },
      { ref: "location:crypt", kind: "location", name: "crypt" },
      { ref: "item:crypt-key", kind: "item", name: "Ключ от склепа" },
      {
        ref: "encounter:crypt-ghouls",
        kind: "encounter",
        name: "crypt-ghouls",
      },
    ]);
  });

  test("бой — Encounter движка с группами монстров, зоной героев и боссом", () => {
    expect(keys(encounterOf())).toEqual(
      [
        "id",
        "location_id",
        "trigger",
        "tactics",
        "monsters",
        "pc_zone",
        "boss",
      ].sort(),
    );
    expect(keys(monsterGroupOf())).toEqual(
      ["monster_id", "count", "zone"].sort(),
    );
    expect(encounterOf().pc_zone).toBe("here");
    expect(encounterOf().boss).toBe(false);
  });

  test("свой статблок — MonsterDef движка со всеми полями; действие — MonsterAction", () => {
    expect(keys(monsterOf())).toEqual(
      [
        "id",
        "name",
        "name_en",
        "cr",
        "xp",
        "ac",
        "hp",
        "hp_dice",
        "speed",
        "scores",
        "saves",
        "skills",
        "resistances",
        "vulnerabilities",
        "immunities",
        "actions",
        "traits",
        "tags",
      ].sort(),
    );
    expect(keys(monsterOf().scores)).toEqual(
      ["str", "dex", "con", "int", "wis", "cha"].sort(),
    );
    expect(keys(actionOf())).toEqual(
      [
        "id",
        "name",
        "kind",
        "attack_bonus",
        "damage",
        "damage_type",
        "range",
        "save",
        "dc",
        "half_on_save",
        "targets",
        "condition",
        "multiattack",
        "summary",
        "reach",
        "secondary_damage",
        "secondary_damage_type",
      ].sort(),
    );
  });

  test("откровение — Revelation с уликами {id, description, where}", () => {
    expect(keys(revelationOf())).toEqual(["id", "statement", "clues"].sort());
    expect(keys(clueOf())).toEqual(["id", "description", "where"].sort());
  });

  test("справочник SRD — {id, name, cr, xp, tags}: полей size/type нет", () => {
    expect(keys(srdMonsterOf())).toEqual(
      ["id", "name", "cr", "xp", "tags"].sort(),
    );
  });

  test("отчёт проверки — DraftReport; находка — {code, message, ref, step}", () => {
    expect(keys(reportOf())).toEqual(
      [
        "errors",
        "warnings",
        "score",
        "estimated_minutes",
        "readiness_percent",
      ].sort(),
    );
    expect(keys(findingOf())).toEqual(
      ["code", "message", "ref", "step"].sort(),
    );
  });
});
