// Free OpenRouter models that support tools (the phone agent needs them). Free
// models are rate-limited by OpenRouter: pair them with a backup model.
export const FREE_MODELS = [
  { label: "Gemma 4 31B (free)", model: "openrouter:google/gemma-4-31b-it:free" },
  { label: "Nemotron 3 Ultra (free)", model: "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free" },
];
