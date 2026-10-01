import js from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist/**", "node_modules/**", "src/generated/**"] },
  js.configs.recommended,
  {
    files: ["**/*.ts", "**/*.tsx"],
    extends: [tseslint.configs.strictTypeChecked, tseslint.configs.stylisticTypeChecked],
    languageOptions: {
      globals: { ...globals.browser },
      parserOptions: {
        project: ["./tsconfig.json"],
        tsconfigRootDir: import.meta.dirname,
      },
    },
    rules: {
      "no-alert": "error",
      // The console is allowed to present, never to decide.
      "no-restricted-syntax": [
        "error",
        {
          selector: "CallExpression[callee.name=/^(reserve|commit|dispatch|launch|command)$/]",
          message:
            "The console must not compute a reservation, dispatch or command. Read it from the API.",
        },
      ],
      // API and contract strings are rendered with textContent, never as markup.
      "no-restricted-properties": [
        "error",
        {
          object: "document",
          property: "write",
          message: "Use DOM construction plus textContent. Contract data must not become markup.",
        },
      ],
      "@typescript-eslint/no-unnecessary-type-assertion": "error",
      // Numbers and booleans in template strings are how this console prints measurements.
      "@typescript-eslint/restrict-template-expressions": [
        "error",
        { allowNumber: true, allowBoolean: true, allowNullish: false, allowAny: false },
      ],
      "@typescript-eslint/consistent-type-imports": ["error", { prefer: "type-imports" }],
    },
  },
  // src/generated/** is openapi-typescript output: committed so a checkout builds without
  // running the generator, excluded from the linter because nobody hand-writes it, and
  // still type-checked by `tsc --noEmit`. The "no any" guarantee is enforced instead by
  // `npm run check:generated`, which fails the build if the generated types contain `any`.
  {
    files: ["scripts/**/*.mjs", "**/*.js", "vite.config.ts"],
    ...tseslint.configs.disableTypeChecked,
    languageOptions: { globals: { ...globals.node } },
  },
);
