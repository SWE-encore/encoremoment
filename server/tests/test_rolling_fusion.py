import unittest
from encore_server.fusion import RollingFusion, TabRules, visual_for_hop


class RollingTests(unittest.TestCase):
    def setUp(self):
        self.f = RollingFusion(TabRules())

    def test_three_full_or_six_half_hops(self):
        for audio, visual, count in [(.1, .2, 3), (.05, .1, 6)]:
            self.f.reset()
            for i in range(count-1):
                self.assertFalse(self.f.push(i*480, audio, visual))
            self.assertTrue(self.f.push((count-1)*480, audio, visual))

    def test_no_duplicate_or_stale_credit(self):
        for _ in range(4):
            self.assertFalse(self.f.push(0, .1, .2))
        for i in range(1, 11):
            self.assertFalse(self.f.push(i*480, 0, None))
        self.assertFalse(self.f.push(5280, .1, .2))
        self.assertAlmostEqual(self.f.total, .48)
        self.assertFalse(self.f.push(7000, .1, .2))
        self.assertAlmostEqual(self.f.total, .48)

    def test_disjoint_face_bins_and_separate_signals(self):
        faces = [(0,.1),(100,.8),(470,.2),(480,1)]
        self.assertEqual(visual_for_hop(faces, 240), .2)
        self.assertEqual(visual_for_hop(faces, 720), 1)
        for i in range(20):
            self.assertFalse(self.f.push(i*480, .19 if i % 2 else 0, None if i % 2 else 1))

    def test_full_window_integrates_exactly_five_seconds(self):
        for i in range(30):
            self.f.push(i*480, .1, .2)
        self.assertAlmostEqual(self.f.total, 5.0)
