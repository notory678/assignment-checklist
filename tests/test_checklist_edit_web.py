"""Manual items, saved-item edits, completed styling and follow-up notices over HTTP (TC13~TC16)."""

import os
import re
import unittest
from urllib.parse import urlsplit

import test_web as web_helpers

BODY = '보고서는 PDF로 제출하세요.\n배포 주소를 포함하세요.'


@unittest.skipUnless(os.getenv('CHECKLIST_TEST_URL'), 'requires isolated CHECKLIST_TEST_URL')
class ChecklistEditWebTests(unittest.TestCase):
    request = web_helpers.WebFlowTests.request
    create_notice = web_helpers.WebFlowTests.create_notice
    detail = web_helpers.WebFlowTests.detail

    def add_item(self, notice_id, title, evidence):
        return self.request(f'/notices/{notice_id}/checklist', [('title', title), ('evidence', evidence)])

    def item_routes(self, notice_id, suffix):
        return [urlsplit(route).path for route in re.findall(rf'action="([^"]+/{suffix})"', self.detail(notice_id))]

    def test_manual_item_requires_an_exact_quote(self):
        """TC13 (R12)"""
        notice_id = self.create_notice(BODY)
        code, html, _ = self.add_item(notice_id, '배포 주소', '없는 문장입니다.')
        self.assertEqual(code, 422)
        self.assertIn('그대로 복사', html)
        self.assertEqual(self.add_item(notice_id, '  ', '배포 주소를 포함하세요.')[0], 422)
        self.assertEqual(self.item_routes(notice_id, 'edit'), [])
        code, _, _ = self.add_item(notice_id, '배포 주소 준비', '배포 주소를 포함하세요.')
        self.assertEqual(code, 303)
        detail = self.detail(notice_id)
        self.assertIn('배포 주소 준비', detail)
        self.assertEqual(len(self.item_routes(notice_id, 'edit')), 1)

    def test_saved_item_edit_keeps_completion_and_preparation(self):
        """TC14 (R04): saved items can be renamed and re-quoted; invalid edits change nothing."""
        notice_id = self.create_notice(BODY)
        self.assertEqual(self.add_item(notice_id, '보고서', '보고서는 PDF로 제출하세요.')[0], 303)
        edit = self.item_routes(notice_id, 'edit')[0]
        base = edit.rsplit('/', 1)[0]
        self.assertEqual(self.request(base + '/completion', [('completed', 'true')])[0], 303)
        self.assertEqual(self.request(base + '/preparation', [('submission_url', 'https://example.test/app'),
                                                             ('preparation_note', '시연 순서 메모')])[0], 303)

        code, html, _ = self.request(edit, [('title', '이름 수정'), ('evidence', '원문에 없는 근거')])
        self.assertEqual(code, 422)
        self.assertIn('원문에 없는 근거', html)  # the rejected input stays in the form
        detail = self.detail(notice_id)
        self.assertNotIn('이름 수정', detail)
        self.assertIn('<p class="evidence">보고서는 PDF로 제출하세요.</p>', detail)

        code, _, headers = self.request(edit, [('title', '배포 주소 준비'), ('evidence', '배포 주소를 포함하세요.')])
        self.assertEqual(code, 303)
        self.assertIn('edited=', headers['Location'])
        detail = self.detail(notice_id)
        self.assertIn('배포 주소 준비', detail)
        self.assertIn('<p class="evidence">배포 주소를 포함하세요.</p>', detail)
        self.assertIn('class="checklist-item completed"', detail)
        self.assertIn('1/1 완료', detail)
        self.assertIn('https://example.test/app', detail)
        self.assertIn('시연 순서 메모', detail)

    def test_edit_rejects_other_notice_and_blank_title(self):
        """TC14 (R04, R11)"""
        first = self.create_notice(BODY)
        second = self.create_notice(BODY)
        self.assertEqual(self.add_item(first, '보고서', '보고서는 PDF로 제출하세요.')[0], 303)
        edit = self.item_routes(first, 'edit')[0]
        item_id = edit.split('/')[-2]
        code, _, _ = self.request(f'/notices/{second}/checklist/{item_id}/edit',
                                  [('title', '침범'), ('evidence', '보고서는 PDF로 제출하세요.')])
        self.assertEqual(code, 404)
        self.assertEqual(self.request(edit, [('title', '   '), ('evidence', '보고서는 PDF로 제출하세요.')])[0], 422)
        self.assertIn('>보고서</p>', self.detail(first))

    def test_completed_item_is_highlighted_and_can_be_cleared(self):
        """TC15 (R05)"""
        notice_id = self.create_notice(BODY)
        self.add_item(notice_id, '보고서', '보고서는 PDF로 제출하세요.')
        completion = self.item_routes(notice_id, 'completion')[0]
        self.assertNotIn('checklist-item completed', self.detail(notice_id))
        self.request(completion, [('completed', 'true')])
        self.assertIn('class="checklist-item completed"', self.detail(notice_id))
        self.request(completion, [('completed', 'false')])
        self.assertNotIn('checklist-item completed', self.detail(notice_id))

    def test_follow_up_notice_is_appended_and_keeps_items(self):
        """TC16: a follow-up notice is attached to an existing notice instead of creating a new one."""
        notice_id = self.create_notice(BODY)
        self.add_item(notice_id, '보고서', '보고서는 PDF로 제출하세요.')
        completion = self.item_routes(notice_id, 'completion')[0]
        self.request(completion, [('completed', 'true')])
        count = self.request('/')[1].count('기존 공지에 추가:')

        code, _, headers = self.request('/notices', [('append_to', str(notice_id)), ('title', '발표 안내'),
                                                     ('body', '발표 자료는 PDF로 제출하세요.')])
        self.assertEqual(code, 303)
        self.assertIn('appended=1', headers['Location'])
        detail = self.detail(notice_id)
        self.assertIn('[추가 공지] 발표 안내\n발표 자료는 PDF로 제출하세요.', detail)
        self.assertIn('추가 공지를 붙였습니다', self.request(urlsplit(headers['Location']).path + '?appended=1')[1])
        self.assertIn('class="checklist-item completed"', detail)
        self.assertEqual(self.request('/')[1].count('기존 공지에 추가:'), count)  # no new notice was created
        # The appended sentence is now a valid quote.
        self.assertEqual(self.add_item(notice_id, '발표 자료', '발표 자료는 PDF로 제출하세요.')[0], 303)

    def test_follow_up_notice_errors_do_not_change_the_notice(self):
        """TC16"""
        notice_id = self.create_notice(BODY)
        code, html, _ = self.request('/notices', [('append_to', str(notice_id)), ('title', ''), ('body', '가' * 30_000)])
        self.assertEqual(code, 422)
        self.assertIn('30,000자', html)
        self.assertEqual(self.request('/notices', [('append_to', str(notice_id)), ('body', '   ')])[0], 422)
        self.assertEqual(self.request('/notices', [('append_to', '999999999'), ('body', '추가')])[0], 404)
        self.assertNotIn('[추가 공지]', self.detail(notice_id))

    def test_new_notice_body_has_an_upper_limit(self):
        """R01: notice bodies longer than the extraction limit are rejected, not truncated."""
        code, html, _ = self.request('/notices', [('title', '긴 공지'), ('body', '가' * 30_001)])
        self.assertEqual(code, 422)
        self.assertIn('30,000자', html)
        code, _, _ = self.request('/assignments', json_data={'title': '긴 공지', 'body': '가' * 30_001})
        self.assertEqual(code, 422)


    def test_delete_one_item_keeps_the_others(self):
        """TC19: deleting an item removes only that item; other notices cannot delete it."""
        notice_id = self.create_notice(BODY)
        other = self.create_notice(BODY)
        self.add_item(notice_id, '보고서', '보고서는 PDF로 제출하세요.')
        self.add_item(notice_id, '배포 주소', '배포 주소를 포함하세요.')
        first, second = self.item_routes(notice_id, r'checklist/\d+/delete')
        completion = self.item_routes(notice_id, 'completion')[1]
        self.request(completion, [('completed', 'true')])
        item_id = first.split('/')[-2]
        self.assertEqual(self.request(f'/notices/{other}/checklist/{item_id}/delete', [])[0], 404)
        self.assertEqual(self.request(f'/notices/{notice_id}/checklist/{item_id}/delete')[0], 405)  # GET never deletes

        code, _, headers = self.request(first, [])
        self.assertEqual(code, 303)
        self.assertIn('deleted=1', headers['Location'])
        detail = self.detail(notice_id)
        self.assertNotIn('>보고서</p>', detail)
        self.assertIn('>배포 주소</p>', detail)
        self.assertIn('1/1 완료', detail)
        self.assertIn('항목을 삭제했습니다', self.request(urlsplit(headers['Location']).path + '?deleted=1')[1])
        self.assertEqual(self.request(first, [])[0], 404)

    def test_delete_notice_removes_its_checklist_only(self):
        """TC19: deleting a notice removes its items; other notices stay."""
        notice_id = self.create_notice(BODY)
        other = self.create_notice(BODY)
        self.add_item(notice_id, '보고서', '보고서는 PDF로 제출하세요.')
        self.add_item(other, '남는 항목', '보고서는 PDF로 제출하세요.')
        item_delete = self.item_routes(notice_id, r'checklist/\d+/delete')[0]
        code, _, headers = self.request(f'/notices/{notice_id}/delete', [])
        self.assertEqual(code, 303)
        self.assertEqual(urlsplit(headers['Location']).query, 'deleted=1')
        self.assertIn('공지와 체크리스트를 삭제했습니다', self.request('/?deleted=1')[1])
        self.assertEqual(self.request(f'/notices/{notice_id}')[0], 404)
        self.assertEqual(self.request(f'/assignments/{notice_id}')[0], 404)
        self.assertEqual(self.request(item_delete, [])[0], 404)
        self.assertEqual(self.request(f'/notices/{notice_id}/delete', [])[0], 404)
        self.assertIn('남는 항목', self.detail(other))


if __name__ == '__main__':
    unittest.main()
