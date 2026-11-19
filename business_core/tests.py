from django.test import TestCase
from rest_framework.test import APIClient
from .models import BusinessUnit, Branch, User


class OwnerBusinessSetupTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner = User.objects.create_user(
            username='owner_test',
            email='owner@fringe.local',
            password='MySecurePass2026!',
            role=User.ROLE_OWNER,
            is_staff=True,
            is_superuser=True,
        )

    def test_owner_two_step_onboarding_businesses_then_branches(self):
        # 1. Login before any BusinessUnit exists -> both setup flags True
        login_res = self.client.post(
            '/api/auth/login/',
            {'username': 'owner_test', 'password': 'MySecurePass2026!'},
            format='json',
        )
        self.assertEqual(login_res.status_code, 200)
        self.assertTrue(login_res.data['requires_business_setup'])
        self.assertTrue(login_res.data['requires_branch_setup'])
        self.assertEqual(login_res.data['available_units'], [])

        token = login_res.data['token']
        self.client.credentials(
            HTTP_AUTHORIZATION=f'Token {token}',
            HTTP_X_BUSINESS_UNIT='bar',
        )

        # 2. /auth/me/ also reports both flags True
        me_res = self.client.get('/api/auth/me/')
        self.assertEqual(me_res.status_code, 200)
        self.assertTrue(me_res.data['requires_business_setup'])
        self.assertTrue(me_res.data['requires_branch_setup'])

        # 3. Attempting to create a branch before setup returns 400 validation error
        branch_fail = self.client.post(
            '/api/core/branches/',
            {'business_unit': 'bar', 'name': 'Main Lounge', 'code': 'ML-01'},
            format='json',
        )
        self.assertEqual(branch_fail.status_code, 400)

        # 4. Step 1: Initialize both 'bar' and 'stationery'
        setup_res = self.client.post(
            '/api/core/business-units/setup-initial/',
            {
                'bar': {
                    'name': 'Fringe Lounge & Bar',
                    'code': 'BAR',
                    'currency': 'TSH',
                    'settings': {'age_verification': True, 'allow_tabs': True},
                },
                'stationery': {
                    'name': 'Fringe Stationery & Retail',
                    'code': 'STAT',
                    'currency': 'TSH',
                    'settings': {'allow_wholesale': True},
                },
            },
            format='json',
        )
        self.assertEqual(setup_res.status_code, 200)
        self.assertFalse(setup_res.data['requires_business_setup'])
        self.assertEqual(BusinessUnit.objects.filter(id__in=['bar', 'stationery']).count(), 2)

        # 5. /auth/me/ now reports requires_business_setup=False, requires_branch_setup=True
        me_step2 = self.client.get('/api/auth/me/')
        self.assertEqual(me_step2.status_code, 200)
        self.assertFalse(me_step2.data['requires_business_setup'])
        self.assertTrue(me_step2.data['requires_branch_setup'])

        # 6. Step 2: Duplicate branch codes rejected
        dup_branch = self.client.post(
            '/api/core/branches/setup-initial/',
            {
                'bar': {'name': 'Main Lounge', 'code': 'BR-01'},
                'stationery': {'name': 'Main Shop', 'code': 'br-01'},
            },
            format='json',
        )
        self.assertEqual(dup_branch.status_code, 400)

        # 7. Step 2: Create one branch for each business
        branch_setup = self.client.post(
            '/api/core/branches/setup-initial/',
            {
                'bar': {'name': 'Mikocheni Lounge', 'code': 'BAR-01', 'address': 'Mikocheni'},
                'stationery': {'name': 'Posta Shop', 'code': 'STAT-01', 'address': 'Posta'},
            },
            format='json',
        )
        self.assertEqual(branch_setup.status_code, 201)
        self.assertFalse(branch_setup.data['requires_branch_setup'])
        self.assertEqual(Branch.objects.count(), 2)

        # 8. /auth/me/ now reports both flags False
        me_done = self.client.get('/api/auth/me/')
        self.assertEqual(me_done.status_code, 200)
        self.assertFalse(me_done.data['requires_business_setup'])
        self.assertFalse(me_done.data['requires_branch_setup'])
