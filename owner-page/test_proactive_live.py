"""Live weather replacement preserves Laura's snow-check controls and learning thresholds."""
import unittest
from datetime import date
from unittest.mock import patch

import proactive


class LiveWeather(unittest.TestCase):
    def setUp(self):
        self.entity = {'c_segment': 'ski_rental', 'address': {'city': 'Levi'}}
        self.today = date(2026, 11, 7)
        self.day = date(2026, 11, 8)
        self.rows = {self.day: {'is_forecast': '1', 'snow_depth_cm': '25', 'snowfall_cm': '12'}}

    def check(self, **kwargs):
        return proactive.weather_check('TEST', self.entity, lambda day: '09:00-17:00',
                                       forecast=self.rows, decision_date=self.today, **kwargs)

    def test_live_snowfall_feeds_existing_question_without_generated_hours(self):
        row = self.check()
        self.assertEqual(row['source'], 'Open-Meteo live forecast')
        self.assertEqual(row['hours'], row['shown'])
        self.assertEqual(row['question'], 'Will you be open as usual?')
        self.assertIn('12 cm of new snow', row['intro'])
        self.assertNotIn('almost always', row['intro'])

    def test_empty_live_source_never_falls_back_to_synthetic_weather(self):
        self.rows = {}
        with patch.object(proactive, '_weather', side_effect=AssertionError('synthetic fallback')):
            self.assertIsNone(self.check())

    def test_snow_depth_growth_is_not_called_new_snow(self):
        self.rows[self.today] = {'is_forecast': '1', 'snow_depth_cm': '0', 'snowfall_cm': '0'}
        self.rows[self.day]['snowfall_cm'] = '2'
        self.assertIsNone(self.check())

    def test_missing_depth_or_snowfall_is_not_zero(self):
        for depth, snowfall in [('', '12'), ('nan', '12'), ('25', ''), ('25', None)]:
            self.rows[self.day].update(snow_depth_cm=depth, snowfall_cm=snowfall)
            with self.subTest(depth=depth, snowfall=snowfall): self.assertIsNone(self.check())

    def test_owner_threshold_preferences_and_business_type_are_preserved(self):
        self.rows[self.day]['snowfall_cm'] = '6'
        self.assertIsNone(self.check())
        self.assertIsNotNone(self.check(score=1))
        self.assertIsNone(self.check(score=-1))
        self.assertIsNone(self.check(score=1, on=False))
        self.entity['c_segment'] = 'cafe'
        self.assertIsNone(self.check(score=1))

    def test_no_snow_check_keeps_owner_hours_without_empirical_claim(self):
        self.rows[self.day].update(snow_depth_cm='0', snowfall_cm='0')
        check = self.check()
        self.assertEqual(check['question'], 'Are you open that day?')
        self.assertEqual(check['hours'], '09:00-17:00')
        self.assertNotIn('usually closed', check['intro'])
