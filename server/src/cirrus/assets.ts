import paths from "../../.cirrus/assets.json";

export type AssetName = keyof typeof paths;
export type Assets = Readonly<Record<AssetName, string>>;

/** Site-relative paths for the fingerprinted assets. */
export const assets: Assets = paths;
