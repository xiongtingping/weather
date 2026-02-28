import { ethers } from "ethers";

function parseArgs(argv) {
  const out = { _: [] };
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];
    if (token.startsWith("--")) {
      const key = token.slice(2);
      const value = argv[i + 1];
      out[key] = value;
      i += 1;
    } else {
      out._.push(token);
    }
  }
  return out;
}

function env(name, fallback = "") {
  return process.env[name] || fallback;
}

function requireEnv(name) {
  const value = env(name);
  if (!value) {
    throw new Error(`missing_env:${name}`);
  }
  return value;
}

async function main() {
  const argv = process.argv.slice(2);
  const args = parseArgs(argv);

  const spender = args.spender || args.to || "";
  const amountRaw = args.amount || "";
  if (!spender || !amountRaw) {
    throw new Error("usage: node scripts/approve_usdc.mjs --spender <address> --amount <number>");
  }

  const rpcUrl = env("POLYGON_RPC_URL", "https://polygon-rpc.com");
  const usdcAddress = env("USDC_ADDRESS", "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359");
  const minGasGwei = env("POLYGON_MIN_GAS_GWEI", "30");

  const provider = new ethers.providers.JsonRpcProvider(rpcUrl);
  const wallet = new ethers.Wallet(requireEnv("PRIVATE_KEY"), provider);
  const abi = [
    "function approve(address spender, uint256 amount) returns (bool)",
    "function allowance(address owner, address spender) view returns (uint256)",
    "function decimals() view returns (uint8)",
  ];
  const usdc = new ethers.Contract(usdcAddress, abi, wallet);
  const decimals = await usdc.decimals();
  const amount = ethers.utils.parseUnits(String(amountRaw), decimals);

  const current = await usdc.allowance(wallet.address, spender);
  if (current.gte(amount)) {
    console.log(JSON.stringify({ ok: true, alreadyApproved: true, spender, amount: amountRaw }));
    return;
  }

  const suggested = await provider.getGasPrice();
  const minGas = ethers.utils.parseUnits(String(minGasGwei), "gwei");
  const gasPrice = suggested.gt(minGas) ? suggested : minGas;

  const tx = await usdc.approve(spender, amount, { gasPrice });
  console.log(JSON.stringify({ ok: true, txHash: tx.hash, from: wallet.address, spender, amount: amountRaw }));
}

main().catch((err) => {
  const message = err && err.message ? err.message : String(err || "error");
  console.error(JSON.stringify({ ok: false, error: message }));
  process.exit(1);
});
