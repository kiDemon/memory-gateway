# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased] - 2026-06-08

### Fixed
- **Dashboard 记忆 ID 搜索**：修复在记忆浏览器中输入记忆 UUID 无法精确查询的问题
  - 后端 API 现在自动检测 UUID 格式，使用 `id=?` 精确匹配
  - 前端搜索框提示更新为"支持输入记忆ID精确查找"

### Changed
- **README 更新**：添加记忆浏览器功能说明
