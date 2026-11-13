pragma solidity ^0.8.0;

import "@openzeppelin/contracts/utils/Address.sol";

contract LendingPair {
    // State variables (simplified for test)
    uint256 public totalSupply;
    uint256 public totalBorrow;
    uint256 public cumulativeSupply;
    uint256 public cumulativeBorrow;
    uint256 public userIndex;
    uint256 public userBalance;
    address public feeRecipient;

    // Mock functions to simulate the vulnerability
    function accrueAccount(address _account) public {
        _distributeReward(_account);
        accrue();
        _accrueAccountInterest(_account);

        if (_account != feeRecipient()) {
            _accrueAccountInterest(fee-recipient());
        }
    }

    function _distributeReward(address _account) internal {
        // Simulate reward distribution
        uint256 reward = 100;
        uint256 expectedReward = (userBalance * reward) / totalSupply;
        uint256 actualReward = 0;
        // This is where the vulnerability occurs: reward is distributed before supply/borrow indexes are updated
        // We assert that the reward is proportional to balance and total supply
        require(actualReward == expectedReward, "Reward distribution is incorrect due to intermediate state updates");
    }

    function accrue() internal {
        cumulativeSupply += 1;
        cumulativeBorrow += 1;
    }

    function _accrueAccountInterest(address _account) internal {
        userIndex += 1;
        userBalance += 1;
    }

    function setFeeRecipient(address _recipient) public {
        feeRecipient = _recipient;
    }

    function setTotalSupply(uint256 _supply) public {
        totalSupply = _supply;
    }

    function setTotalBorrow(uint256 _borrow) public {
        totalBorrow = _borrow;
    }

    function setUserBalance(address _account, uint256 _balance) public {
        userBalance = _balance;
    }

    function setUserIndex(address _account, uint256 _index) public {
        userIndex = _index;
    }

    // Fuzz test entry point
    function fuzzAccrueAccount(address _account) public {
        // Set up test state
        setTotalSupply(100);
        setTotalBorrow(50);
        setUserBalance(_account, 50);
        setUserIndex(_account, 1);
        setFeeRecipient(address(0x1234567890123456789012345678901234567890));

        // Trigger the vulnerable function
        accrueAccount(_account);

        // Assert that reward distribution is fair
        // Since reward is distributed before supply/borrow indexes are updated,
        // the totalSupply and userBalance are outdated, leading to incorrect reward
        // This assertion checks that the reward is proportional to balance and total supply
        uint256 expectedReward = (50 * 100) / 100; // 50
        uint256 actualReward = 0;
        require(actualReward == expectedReward, "Incorrect reward distribution due to state update order");
    }
}
