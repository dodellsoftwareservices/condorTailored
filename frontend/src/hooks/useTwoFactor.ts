import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  confirmTwoFactor,
  disableTwoFactor,
  enrollTwoFactor,
  getTwoFactorStatus,
  type TwoFactorStatus,
} from "@/lib/twoFactorApi";

export const TWO_FACTOR_KEY = ["two-factor-status"] as const;

export function useTwoFactorStatus() {
  return useQuery({
    queryKey: TWO_FACTOR_KEY,
    queryFn: getTwoFactorStatus,
    staleTime: 60 * 1000,
  });
}

export function useEnrollTwoFactor() {
  return useMutation({ mutationFn: enrollTwoFactor });
}

export function useConfirmTwoFactor() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: confirmTwoFactor,
    onSuccess: (res: TwoFactorStatus) => qc.setQueryData(TWO_FACTOR_KEY, res),
  });
}

export function useDisableTwoFactor() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: disableTwoFactor,
    onSuccess: (res: TwoFactorStatus) => qc.setQueryData(TWO_FACTOR_KEY, res),
  });
}
