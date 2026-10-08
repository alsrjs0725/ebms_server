## 2023-10-27 - Context-aware ARIA labels in repetitive lists
**Learning:** List action buttons (like "Disconnect", "Logout", "Save" in a user row or device row) sound confusing to screen reader users if they all have the exact same label.
**Action:** When adding actions in lists, always use variables from the surrounding row to generate a unique, context-aware `aria-label` (e.g., `aria-label="{{ u.display_name }} 설정 저장"`).
