"""웹 화면(web/index.html)의 핵심 사용자 시나리오 브라우저 자동 테스트.

`tests/`의 단위 테스트와 달리 실제 브라우저(Playwright)와 실제 외부
API 키(.env)가 필요하다. 그래서 `python -m unittest discover -s tests`
에는 포함되지 않으며, 아래처럼 별도 실행한다.

    pip install playwright
    python -m playwright install chromium
    python -m unittest discover -s tests_e2e -v
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

from plan_b_api.cli import load_env_file
from plan_b_api.server import create_server


class WebUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        load_env_file()
        cls._temp_dir = tempfile.TemporaryDirectory()
        cache_path = Path(cls._temp_dir.name) / "cache.sqlite3"
        cls.server = create_server("127.0.0.1", 0, str(cache_path))
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.server_thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True
        )
        cls.server_thread.start()

        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=2)
        cls._temp_dir.cleanup()

    def setUp(self) -> None:
        self.page = self.browser.new_page()
        self.page.goto(self.base_url + "/")
        # /priorities 응답이 도착해 렌더링될 때까지 기다려 서버 연결과
        # 초기화(init())가 끝났음을 확인한다. 이 칩들은 접힌 <details>
        # 안에 있어 화면엔 안 보일 수 있으니 DOM에 붙었는지만 확인한다.
        self.page.wait_for_selector(
            "#priority-list .priority-chip", state="attached", timeout=10_000
        )

    def tearDown(self) -> None:
        self.page.close()

    # ---------- 헬퍼 ----------

    def _search_and_add(self, keyword: str) -> None:
        self.page.fill("#search-q", keyword)
        self.page.click("#btn-search")
        self.page.wait_for_selector(
            "#search-results .search-result", timeout=10_000
        )
        self.page.click(
            "#search-results .search-result >> nth=0 >> text=일정에 추가"
        )

    def _schedule_titles(self) -> list[str]:
        return self.page.locator("#schedule-list .schedule-item .title").all_inner_texts()

    def _drag_reorder_first_two(self) -> None:
        # Chromium은 CDP로 합성한 마우스 이벤트만으로는 네이티브 HTML5
        # dragstart를 신뢰성 있게 발생시키지 않는다(알려진 제약). 그래서
        # 실제 브라우저 제스처를 흉내내는 대신, 우리 앱의 dragstart/
        # dragover/drop 리스너를 DragEvent로 직접 호출해서 검증한다.
        self.page.eval_on_selector_all(
            "#schedule-list .schedule-item",
            """(items) => {
                const source = items[0].querySelector('.drag-handle');
                const target = items[1];
                const dataTransfer = new DataTransfer();
                const rect = target.getBoundingClientRect();
                const opts = { bubbles: true, cancelable: true, dataTransfer };
                source.dispatchEvent(new DragEvent('dragstart', opts));
                target.dispatchEvent(new DragEvent('dragover', {
                    ...opts, clientY: rect.bottom - 2,
                }));
                target.dispatchEvent(new DragEvent('drop', {
                    ...opts, clientY: rect.bottom - 2,
                }));
                source.dispatchEvent(new DragEvent('dragend', opts));
            }""",
        )

    # ---------- 테스트 ----------

    def test_mobile_viewport_shows_map_and_draggable_sheets_without_overflow(self) -> None:
        # 네이버 지도 스타일 개편 후: 지도는 항상 전체 화면 배경이고
        # 일정/경로 상세/여행 정보는 손잡이가 달린 바텀시트 하나에
        # 탭 세 개로 합쳐져 있다(시트가 두 개 겹쳐 뜨는 방식은 제거됨).
        self.page.set_viewport_size({"width": 390, "height": 844})

        overflow = self.page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        self.assertEqual(overflow, 0)
        self.assertTrue(self.page.is_visible("#map"))
        self.assertFalse(self.page.is_visible("#sidebar"))
        self.assertTrue(self.page.is_visible("#directions-panel"))
        self.assertTrue(self.page.is_visible("#directions-handle"))
        self.assertTrue(self.page.is_visible('#panel-tabs .panel-tab[data-view="info"]'))

        self.page.click('#panel-tabs .panel-tab[data-view="info"]')
        self.assertTrue(self.page.is_visible("#trip-title"))
        self.assertTrue(self.page.is_visible("#search-q"))

        self.page.click('#panel-tabs .panel-tab[data-view="schedule"]')
        self.assertFalse(self.page.is_visible("#trip-title"))
        self.assertTrue(self.page.is_visible("#btn-save"))

    def test_search_and_add_to_schedule(self) -> None:
        self._search_and_add("경복궁")
        self.page.wait_for_selector("#schedule-list .schedule-item")
        self.assertEqual(self.page.locator("#schedule-count").inner_text(), "1")
        self.assertIn("경복궁", self._schedule_titles()[0])

    def test_lock_disables_delete_and_replace_buttons(self) -> None:
        self._search_and_add("경복궁")
        self.page.wait_for_selector("#schedule-list .schedule-item")
        item = self.page.locator("#schedule-list .schedule-item").first
        self.assertFalse(item.locator(".f-remove").is_disabled())

        item.locator(".f-locked").check()
        self.assertTrue(item.locator(".f-remove").is_disabled())
        self.assertTrue(item.locator(".f-gap").is_disabled())

        item.locator(".f-locked").uncheck()
        self.assertFalse(item.locator(".f-remove").is_disabled())

    def test_remove_schedule_item(self) -> None:
        self._search_and_add("경복궁")
        self.page.wait_for_selector("#schedule-list .schedule-item")
        self.page.click("#schedule-list .schedule-item .f-remove")
        self.assertEqual(self.page.locator("#schedule-count").inner_text(), "0")

    def test_drag_reorder_changes_order(self) -> None:
        self._search_and_add("경복궁")
        self.page.wait_for_selector("#schedule-list .schedule-item")
        self._search_and_add("서울시청")
        self.page.wait_for_function(
            "document.querySelectorAll('#schedule-list .schedule-item').length === 2"
        )

        before = self._schedule_titles()
        self.assertEqual(len(before), 2)

        self._drag_reorder_first_two()

        after = self._schedule_titles()
        self.assertEqual(after, list(reversed(before)))

    def test_map_visibility_toggle_hides_route_line(self) -> None:
        self._search_and_add("경복궁")
        self.page.wait_for_selector("#schedule-list .schedule-item")
        self._search_and_add("서울시청")
        self.page.wait_for_function(
            "document.querySelectorAll('#schedule-list .schedule-item').length === 2"
        )

        self.page.click(".leg .f-walk")
        self.page.wait_for_function(
            "document.querySelector('.leg .f-leg-visible') !== null"
        )
        # 경로를 조회하면 "경로 상세" 탭으로 자동 전환된다 — 구간 표시
        # 체크박스는 "일정" 탭 안에 있으므로 눌러보려면 다시 돌아와야 한다.
        self.page.click("#panel-tabs button[data-view=schedule]")
        self.page.wait_for_selector(".leg .f-leg-visible", state="visible", timeout=15_000)
        self.page.wait_for_function(
            "document.querySelectorAll('.leaflet-overlay-pane path').length > 0",
            timeout=15_000,
        )

        self.page.click(".leg .f-leg-visible")
        self.page.wait_for_function(
            "document.querySelectorAll('.leaflet-overlay-pane path').length === 0"
        )

        self.page.click(".leg .f-leg-visible")
        self.page.wait_for_function(
            "document.querySelectorAll('.leaflet-overlay-pane path').length > 0"
        )

    def test_reorder_auto_recomputes_leg_route_in_last_used_mode(self) -> None:
        # 티맵처럼, 순서를 바꾸면 마지막으로 쓰던 이동수단(도보)으로
        # 구간 경로가 자동으로 다시 계산돼야 한다(수동 재조회 불필요).
        self._search_and_add("경복궁")
        self.page.wait_for_selector("#schedule-list .schedule-item")
        self._search_and_add("서울시청")
        self.page.wait_for_function(
            "document.querySelectorAll('#schedule-list .schedule-item').length === 2"
        )

        self.page.click(".leg .f-walk")
        self.page.wait_for_function(
            "document.querySelector('.leg .f-leg-visible') !== null"
        )
        # 경로 조회·재조회는 "경로 상세" 탭을 자동으로 여닫기 때문에, 탭
        # 상태와 무관하게 실제 DOM 내용(textContent)으로 결과를 확인한다.
        self.assertIn(
            "도보", self.page.evaluate("document.querySelector('.leg').textContent")
        )

        self._drag_reorder_first_two()

        self.page.wait_for_function(
            "document.querySelector('.leg').textContent.includes('분')",
            timeout=15_000,
        )
        self.assertIn(
            "도보", self.page.evaluate("document.querySelector('.leg').textContent")
        )
        self.assertNotIn(
            "미조회", self.page.evaluate("document.querySelector('.leg').textContent")
        )

    def test_save_creates_share_link_and_reload_restores_schedule(self) -> None:
        self.page.fill("#trip-title", "이-테스트-여행")
        self._search_and_add("경복궁")
        self.page.wait_for_selector("#schedule-list .schedule-item")

        self.page.click("#btn-save")
        self.page.wait_for_selector("#share-box[style*='block']", timeout=10_000)
        share_url = self.page.input_value("#share-url")
        self.assertIn("?trip=", share_url)

        reloaded = self.browser.new_page()
        try:
            reloaded.goto(share_url)
            reloaded.wait_for_selector("#schedule-list .schedule-item")
            self.assertEqual(
                reloaded.input_value("#trip-title"), "이-테스트-여행"
            )
            titles = reloaded.locator(
                "#schedule-list .schedule-item .title"
            ).all_inner_texts()
            self.assertIn("경복궁", titles[0])
        finally:
            reloaded.close()


if __name__ == "__main__":
    unittest.main()
