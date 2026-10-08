"""Numerical identities, physical direction and support boundaries."""
import unittest
import numpy as np
from strict_fit import weighted_p,PF,cloud_x


class IncrementBoundaries(unittest.TestCase):
    def setUp(self):
        rng=np.random.default_rng(20261007)
        self.p=np.exp(rng.uniform(-700,0,(1000,15)))
        self.live=rng.random(self.p.shape)>.25;self.live[:,0]=True
        self.p=np.where(self.live,self.p,0);self.p/=self.p.sum(1,keepdims=True)
        self.rise=np.broadcast_to(np.arange(15),self.p.shape)
        self.base=PF._served_vectors(self.p,self.live)

    def test_zero_strength_exact_identity_including_underflow(self):
        np.testing.assert_array_equal(weighted_p(self.p,self.live,-self.rise,0),self.base)

    def test_absent_and_zero_increment_exact_identity(self):
        np.testing.assert_array_equal(weighted_p(self.p,self.live,np.zeros_like(self.p),4),self.base)

    def test_direction_both_ways_and_extreme_coefficients(self):
        for beta in (.01,.5,1,4):
            for sign in (-1,1):
                q=weighted_p(self.p,self.live,sign*self.rise,beta)
                np.testing.assert_allclose(q.sum(1),1,atol=1e-14)
                self.assertTrue((q[~self.live]==0).all())
                # The production floor can perturb means near machine-scale
                # tails; allow only its known numerical mass bound.
                delta=(q*self.rise).sum(1)-(self.base*self.rise).sum(1)
                self.assertGreaterEqual(float((sign*delta).min()),-2e-4)
                # Unsaturated odds must obey the exact likelihood ratio.
                for a,b in ((0,1),(3,9),(10,14)):
                    ok=self.live[:,a]&self.live[:,b]&(self.p[:,a]>.001)&(self.p[:,b]>.001)&(q[:,a]>.001)&(q[:,b]>.001)
                    got=np.log(q[ok,a]/q[ok,b])-np.log(self.p[ok,a]/self.p[ok,b])
                    np.testing.assert_allclose(got,sign*beta*(a-b),atol=2e-12)

    def test_floor_applied_once_against_independent_formula(self):
        p=np.array([[.5,.5,1e-80,0.]])
        p/=p.sum(1,keepdims=True);live=np.array([[1,1,1,0]],bool);x=np.array([[0.,-1,-2,0.]])
        q=p*np.exp(2*x);q/=q.sum(1,keepdims=True)
        q=np.where(live,np.maximum(q,PF.PROBABILITY_MODEL_ZERO_FLOOR),0);q/=q.sum(1,keepdims=True)
        np.testing.assert_allclose(weighted_p(p,live,x,2),q,rtol=2e-15,atol=1e-16)

    def test_invalid_support_fails_closed(self):
        for p,live,x,beta in (([[1.,0]],[[0,1]],[[0,0]],1),([[1.,0]],[[1,0]],[[np.nan,0]],1),([[.5,0]],[[1,0]],[[0,0]],1),([[1.,0]],[[1,0]],[[0,0]],np.inf)):
            with self.assertRaises(ValueError):weighted_p(p,live,x,beta)

    def test_cloud_unknown_zero_and_stale(self):
        r=dict(cloud_observation_origin_eligible=True,as_of='2026-10-01T10:00:00Z',cloud_observation_valid_at='2026-10-01T08:00:00Z',cloud_pairs={'latest':dict(known_at='2026-10-01T07:00:00Z',coverage_increment={'6500':0},base_lowering_km=None)},sample=dict(running='a',view=dict(ordinals={'a':0,'b':1},centers={'a':25.,'b':26.})))
        x,k=cloud_x([r],2,'latest',6500,'coverage');self.assertTrue(k[0]);np.testing.assert_array_equal(x,0)
        x,k=cloud_x([r],2,'latest',6500,'base');self.assertFalse(k[0])
        r['cloud_observation_valid_at']='2026-10-01T07:59:59Z'
        x,k=cloud_x([r],2,'latest',6500,'coverage');self.assertFalse(k[0])

    def test_cloud_origin_explicitly_required(self):
        r=dict(as_of='2026-10-01T10:00:00Z',cloud_observation_valid_at='2026-10-01T10:00:00Z',cloud_pairs={'latest':dict(known_at='2026-10-01T07:00:00Z',coverage_increment={'6500':.25},base_lowering_km=None)},sample=dict(running='a',view=dict(ordinals={'a':0,'b':1},centers={'a':25.,'b':26.})))
        for flag in ('missing',False,None):
            if flag!='missing':r['cloud_observation_origin_eligible']=flag
            x,k=cloud_x([r],2,'latest',6500,'coverage');self.assertFalse(k[0]);np.testing.assert_array_equal(x,0)
        r['cloud_observation_origin_eligible']=True;x,k=cloud_x([r],2,'latest',6500,'coverage');self.assertTrue(k[0]);self.assertEqual(x[0,1],-.25)
        r['cloud_observation_valid_at']='2026-10-01T10:00:01Z'
        x,k=cloud_x([r],2,'latest',6500,'coverage');self.assertFalse(k[0])
        r['cloud_observation_valid_at']='2026-10-01T08:00:00Z';r['cloud_pairs']['latest']['known_at']='2026-10-01T08:00:01Z'
        x,k=cloud_x([r],2,'latest',6500,'coverage');self.assertFalse(k[0])


if __name__=='__main__':unittest.main()
