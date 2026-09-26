import { defineConfig } from "@neon/config/v1";

export default defineConfig({
  preview: {
    buckets: {
      docs: { access: "private" },
      reports: { access: "private" },
    },
  },
});
