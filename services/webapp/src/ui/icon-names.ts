/** Иконки набора: кубик, свеча, свиток, щит, сердце, песочные часы, печать. */
export const ICON_NAMES = [
  "d20",
  "candle",
  "scroll",
  "shield",
  "heart",
  "hourglass",
  "seal",
] as const;

export type IconName = (typeof ICON_NAMES)[number];
