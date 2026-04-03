import { z } from "zod";

// ---------------------------------------------------------------------------
// Settings map — the API returns a plain string→string dict
// ---------------------------------------------------------------------------

export const SettingsMapSchema = z.record(z.string(), z.string());

export type SettingsMap = z.infer<typeof SettingsMapSchema>;

// ---------------------------------------------------------------------------
// Single setting entry (for table display)
// ---------------------------------------------------------------------------

export const SettingEntrySchema = z.object({
  key: z.string(),
  value: z.string(),
});

export type SettingEntry = z.infer<typeof SettingEntrySchema>;
