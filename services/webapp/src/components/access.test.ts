import type { CombatView, GamePhase, PhaseView, TurnPhase } from "../api/types";
import {
  CONNECTION_LOST_NOTE,
  cannotAct,
  composerAccess,
  connectionLost,
  currentFighter,
  isHeroTurn,
} from "./access";
import {
  combatOf,
  combatWith,
  heroFighter,
  monsterFighter,
  phaseOf,
} from "./fixtures";

const HERO = "pc_lira";

const access = (
  phase: PhaseView,
  combat: CombatView | null = null,
  characterId: string | null = HERO,
) => composerAccess(phase, combat, characterId);

describe("поле хода: когда оно доступно", () => {
  test.each(["discussion", "collecting"] as const)(
    "идёт игра, фаза %s — поле открыто",
    (turnPhase) => {
      expect(access(phaseOf(turnPhase))).toEqual({ open: true, note: null });
    },
  );

  test.each([
    ["resolving", /Мастер думает/],
    ["dm_failed", /Мастер не справился/],
    ["camp", /Привал/],
    [null, /пока не принимаются/],
  ] as const)(
    "игра идёт, фаза хода %s — поле закрыто, причина названа",
    (turnPhase, note) => {
      const result = access(phaseOf(turnPhase as TurnPhase | null));

      expect(result.open).toBe(false);
      expect(result.note).toMatch(note);
    },
  );

  test.each(["discussion", "collecting", "resolving", "combat_wait"] as const)(
    "пауза закрывает поле в любой фазе хода (%s)",
    (turnPhase) => {
      const result = access(phaseOf(turnPhase, { phase: "paused" }));

      expect(result.open).toBe(false);
      expect(result.note).toMatch(/пауз/i);
    },
  );

  test.each([
    ["lobby", /не началась/],
    ["weaving", /не началась/],
    ["finished", /окончена/],
  ] as const)("игра в фазе %s — поле закрыто", (phase, note) => {
    const result = access(phaseOf("discussion", { phase: phase as GamePhase }));

    expect(result.open).toBe(false);
    expect(result.note).toMatch(note);
  });
});

describe("поле хода в бою (combat_wait)", () => {
  const waiting = phaseOf("combat_wait");

  test("свой ход по порядку инициативы — поле открыто", () => {
    expect(access(waiting, combatOf(HERO))).toEqual({
      open: true,
      note: null,
    });
  });

  test("ходит монстр — закрыто, назван тот, кто ходит", () => {
    expect(access(waiting, combatOf("skeleton-1"))).toEqual({
      open: false,
      note: "Ходит Скелет.",
    });
  });

  test("ходит чужой герой — закрыто, назван по имени", () => {
    const combat = combatOf("pc_boris");
    combat.order.push({
      id: "pc_boris",
      name: "Борин",
      kind: "pc",
      side: "party",
      zone_id: "hall",
      conditions: [],
      character_id: "pc_boris",
      hp: 8,
      hp_max: 8,
    });

    expect(access(waiting, combat)).toEqual({
      open: false,
      note: "Ходит Борин.",
    });
  });

  test("ход спрятанного монстра (current_id пуст) — закрыто, ходит мастер", () => {
    expect(access(waiting, combatOf(null))).toEqual({
      open: false,
      note: "Ходит мастер.",
    });
  });

  test("боя в данных нет — закрыто", () => {
    expect(access(waiting, null).open).toBe(false);
  });

  test("героя у игрока нет — закрыто", () => {
    expect(access(waiting, combatOf(HERO), null).open).toBe(false);
  });

  test("current_id не из порядка хода — просто «Сейчас не ваш ход»", () => {
    expect(access(waiting, combatOf("ghost"))).toEqual({
      open: false,
      note: "Сейчас не ваш ход.",
    });
  });

  test("пауза в бою закрывает поле даже в свой ход", () => {
    const paused = phaseOf("combat_wait", { phase: "paused" });

    expect(access(paused, combatOf(HERO)).open).toBe(false);
  });
});

describe("кто ходит в бою", () => {
  test("currentFighter: боец по current_id, героя или монстра", () => {
    expect(currentFighter(combatOf(HERO))).toMatchObject({
      id: HERO,
      kind: "pc",
    });
    expect(currentFighter(combatOf("skeleton-1"))).toMatchObject({
      id: "skeleton-1",
      kind: "monster",
    });
  });

  test.each([
    ["боя нет", null],
    ["ходит спрятанный монстр (current_id пуст)", combatOf(null)],
    ["current_id не из порядка хода", combatOf("ghost")],
  ])("currentFighter: %s — никого", (_name, combat) => {
    expect(currentFighter(combat)).toBeNull();
  });

  test("isHeroTurn: да, только когда ходит герой с этим id персонажа", () => {
    expect(isHeroTurn(combatOf(HERO), HERO)).toBe(true);
    expect(isHeroTurn(combatOf("skeleton-1"), HERO)).toBe(false);
    expect(isHeroTurn(combatOf(HERO), "pc_boris")).toBe(false);
  });

  test("isHeroTurn: нет героя, нет боя или ходит спрятанный монстр — нет", () => {
    expect(isHeroTurn(combatOf(HERO), null)).toBe(false);
    expect(isHeroTurn(null, HERO)).toBe(false);
    expect(isHeroTurn(combatOf(null), HERO)).toBe(false);
  });
});

describe("недееспособный герой на очереди (can_act от сервера)", () => {
  const waiting = phaseOf("combat_wait");

  /** Лира и Борин, оба герои; у кого `can_act: false`, тот парализован. Ходит `current`. */
  const duel = (current: string | null, canAct: Record<string, boolean>) =>
    combatWith(current, [
      heroFighter("pc_lira", "Лира", { can_act: canAct.pc_lira }),
      heroFighter("pc_boris", "Борин", { can_act: canAct.pc_boris }),
      monsterFighter("skeleton-1", "Скелет"),
    ]);

  test("мой герой не может действовать — поле закрыто, назван по имени", () => {
    const combat = duel("pc_lira", { pc_lira: false });

    expect(access(waiting, combat)).toEqual({
      open: false,
      note: "Лира не может действовать.",
    });
  });

  test("чужой герой не может действовать — то же пояснение вместо «Ходит …»", () => {
    const combat = duel("pc_boris", { pc_boris: false });

    expect(access(waiting, combat)).toEqual({
      open: false,
      note: "Борин не может действовать.",
    });
  });

  test("герой может действовать (can_act: true) — прежнее поведение", () => {
    expect(access(waiting, duel("pc_lira", { pc_lira: true }))).toEqual({
      open: true,
      note: null,
    });
    expect(access(waiting, duel("pc_boris", { pc_boris: true }))).toEqual({
      open: false,
      note: "Ходит Борин.",
    });
  });

  test("признака нет (старый сервер) — герой считается способным действовать", () => {
    expect(access(waiting, combatOf(HERO)).open).toBe(true);
  });

  test("недееспособен другой герой, не тот, чья очередь, — очередь не затронута", () => {
    const combat = duel("pc_lira", { pc_lira: true, pc_boris: false });

    expect(access(waiting, combat).open).toBe(true);
  });

  test("у монстра признака нет — «Ходит Скелет.», как раньше", () => {
    const combat = duel("skeleton-1", { pc_lira: false });

    expect(access(waiting, combat)).toEqual({
      open: false,
      note: "Ходит Скелет.",
    });
  });

  test("пауза важнее: на паузе пояснение про паузу, а не про героя", () => {
    const paused = phaseOf("combat_wait", { phase: "paused" });
    const combat = duel("pc_lira", { pc_lira: false });

    expect(access(paused, combat).note).toMatch(/пауз/i);
  });

  test("имя героя в пояснении — только текст, без разметки", () => {
    const combat = combatWith("pc_lira", [
      heroFighter("pc_lira", "<b>Лира</b>", { can_act: false }),
    ]);

    expect(access(waiting, combat).note).toBe(
      "<b>Лира</b> не может действовать.",
    );
  });

  test("isHeroTurn: свой герой на очереди, но не может действовать — не ход игрока", () => {
    expect(isHeroTurn(duel("pc_lira", { pc_lira: false }), HERO)).toBe(false);
    expect(isHeroTurn(duel("pc_lira", { pc_lira: true }), HERO)).toBe(true);
    expect(isHeroTurn(duel("pc_lira", {}), HERO)).toBe(true);
  });

  test.each([
    [
      "герой с can_act: false",
      heroFighter("pc_lira", "Лира", { can_act: false }),
      true,
    ],
    [
      "герой с can_act: true",
      heroFighter("pc_lira", "Лира", { can_act: true }),
      false,
    ],
    ["герой без признака", heroFighter("pc_lira", "Лира"), false],
    ["монстр", monsterFighter("skeleton-1", "Скелет"), false],
    ["бойца нет", null, false],
  ])("cannotAct: %s", (_name, fighter, expected) => {
    expect(cannotAct(fighter)).toBe(expected);
  });
});

describe("связь с игрой", () => {
  const LOST = /^Нет связи с игрой/;
  const discussion = phaseOf("discussion");

  test.each(["open", "idle", "connecting"] as const)(
    "связь %s: обрыва нет, поле открывается по фазе, как раньше",
    (connection) => {
      expect(connectionLost(connection)).toBe(false);
      expect(composerAccess(discussion, null, HERO, connection)).toEqual({
        open: true,
        note: null,
      });
    },
  );

  test("связь не названа — как открытая: прежние вызовы не меняются", () => {
    expect(composerAccess(discussion, null, HERO)).toEqual(
      composerAccess(discussion, null, HERO, "open"),
    );
  });

  test("обрыв (reconnecting): поле закрыто, причина названа", () => {
    expect(connectionLost("reconnecting")).toBe(true);

    const result = composerAccess(discussion, null, HERO, "reconnecting");

    expect(result.open).toBe(false);
    expect(result.note).toMatch(LOST);
  });

  test("пояснение об обрыве одно на все поля (ход, реплика у костра): общая константа", () => {
    expect(CONNECTION_LOST_NOTE).toMatch(LOST);
    expect(composerAccess(discussion, null, HERO, "reconnecting").note).toBe(
      CONNECTION_LOST_NOTE,
    );
  });

  test.each(["discussion", "collecting"] as const)(
    "обрыв закрывает и поле, открытое фазой (%s)",
    (turnPhase) => {
      const result = composerAccess(
        phaseOf(turnPhase),
        null,
        HERO,
        "reconnecting",
      );

      expect(result.open).toBe(false);
      expect(result.note).toMatch(LOST);
    },
  );

  test("обрыв закрывает поле и в свой ход в бою", () => {
    const result = composerAccess(
      phaseOf("combat_wait"),
      combatOf(HERO),
      HERO,
      "reconnecting",
    );

    expect(result).toEqual({ open: false, note: expect.stringMatching(LOST) });
  });

  test.each([
    ["пауза", phaseOf("discussion", { phase: "paused" })],
    ["мастер думает", phaseOf("resolving")],
    ["привал", phaseOf("camp")],
    ["игра окончена", phaseOf("discussion", { phase: "finished" })],
  ])(
    "при обрыве причина — связь, а не фаза (%s): фаза в клиенте могла устареть",
    (_name, phase) => {
      const result = composerAccess(phase, null, HERO, "reconnecting");

      expect(result.open).toBe(false);
      expect(result.note).toMatch(LOST);
    },
  );
});
