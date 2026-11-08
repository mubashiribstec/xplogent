// Free OpenRouter models that support tools (the phone agent needs them). Free
// models are rate-limited by OpenRouter: pair them with a backup model.
export const FREE_MODELS = [
  { label: "Gemma 4 31B (free)", model: "openrouter:google/gemma-4-31b-it:free" },
  { label: "Nemotron 3 Ultra (free)", model: "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free" },
];

// Fast models for calls through the Vercel AI Gateway (AI_GATEWAY_API_KEY; the free tier
// includes them). Measured by Muse on 5 Nov: nova-micro ~1.0 s per reply (weakest answers:
// check it on test calls), gpt-4o-mini ~2.2 s. "Test this model" times them on your server.
export const FAST_CALL_MODELS = [
  { label: "Nova Micro (fastest, ~1 s; check answer quality)", model: "vercel:amazon/nova-micro" },
  { label: "GPT-4o mini (fast, ~2 s)", model: "vercel:openai/gpt-4o-mini" },
];
