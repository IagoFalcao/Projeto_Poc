pragma solidity ^0.8.0;

import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/utils/math/Math.sol";

contract ItyFuzzTest {

    // Mock contract to simulate LendingPair behavior
    struct LendingPair {
        uint public cumulativeInterestRate;
        uint public LIQ_MIN_HEALTH;
        address public tokenA;
        address public tokenB;
        address public feeRecipient;
        struct Account {
            uint debt;
            uint health;
            uint interestAccrued;
        } 
        mapping(address => Account) public account;
        mapping(address => mapping(address => uint)) public debtOf;
        mapping(address => uint) public supplyDebt;
        mapping(address => uint) public supplyBurn;
        mapping(address => uint) public systemFee;
        mapping(address => uint) public callerFee;
    }

    LendingPair public lendingPair;

    // Mock IERC20 interface
    IERC20 public tokenA;
    IERC20 public tokenB;

    // Fuzz test: Verify that liquidation does not use outdated interest rate
    function testInterestRateMismatch() external {
        // Setup: Simulate a borrower with a low health (underwater)
        address borrower = address(1);
        address repayToken = address(2);
        address supplyToken = address(3);
        uint repayAmount = 1000;
        uint minSupplyOutput = 900;

        // Set initial interest rate (before any accrual)
        lendingPair.cumulativeInterestRate = 1000; // 10% interest rate

        // Simulate interest accrual before liquidation
        // This should update the cumulativeInterestRate
        lendingPair.account[borrower].interestAccrued = 100;

        // Perform liquidation
        lendingPair._accrueAccountInterest(borrower);
        lendingPair._accrueAccountInterest(lendingPair.feeRecipient);

        // Check if interest has been properly updated
        uint currentInterestRate = lendingPair.cumulativeInterestRate;
        uint expectedInterestRate = 1000 + 100; // 10% + 100 accrued

        // Assert: The interest rate must have been updated
        require(currentInterestRate == expectedInterestRate, "Interest rate not updated after accrual");

        // Simulate health check
        uint health = lendingPair.account[borrower].health;
        require(health < lendingPair.LIQ_MIN_HEALTH, "Account health should be underwater");

        // Perform liquidation
        lendingPair.liquidateAccount(
            borrower,
            repayToken,
            repayAmount,
            minSupplyOutput
        );

        // After liquidation, verify that the interest state is still valid
        // If the interest rate was not updated, the borrower would avoid paying interest
        // This test ensures that the interest rate is updated before liquidation
        require(lendingPair.account[borrower].interestAccrued >= 100, "Interest not accrued before liquidation");

        // Final assertion: The interest rate must reflect the updated value
        require(lendingPair.cumulativeInterestRate >= expectedInterestRate, "Cumulative interest rate not updated");
    }

    // Helper: Mock _accrueAccountInterest
    function _accrueAccountInterest(address account) internal {
        lendingPair.account[account].interestAccrued += 10;
        lendingPair.cumulativeInterestRate += 10;
    }

    // Helper: Mock accountHealth
    function accountHealth(address account) internal view returns (uint) {
        return lendingPair.account[account].health;
    }

    // Helper: Mock _validateToken
    function _validateToken(address token) internal pure {
        // No-op
    }

    // Helper: Mock _convertTokenValues
    function _convertTokenValues(address from, address to, uint amount) internal pure returns (uint) {
        return amount;
    }

    // Helper: Mock _burnSupply
    function _burnSupply(address token, address account, uint amount) internal {
        lendingPair.supplyDebt[account] -= amount;
    }

    // Helper: Mock _mintSupply
    function _mintSupply(address token, address account, uint amount) internal {
        lendingPair.supplyBurn[account] += amount;
    }

    // Helper: Mock _burnDebt
    function _burnDebt(address token, address account, uint amount) internal {
        lendingPair.debtOf[token][account] -= amount;
    }

    // Helper: Mock _safeTransferFrom
    function _safeTransferFrom(address token, address from, uint amount) internal {
        // No-op
    }

    // Helper: Mock _safeTransfer
    function _safeTransfer(IERC20 token, address from, uint amount) internal {
        // No-op
    }

    // Mock controller functions
    function liqFeeCaller(address token) internal pure returns (uint) {
        return 1;
    }

    function liqFeeSystem(address token) internal pure returns (uint) {
        return 1;
    }

    function LIQ_MIN_HEALTH() internal pure returns (uint) {
        return 1000;
    }
}
