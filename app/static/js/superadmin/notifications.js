const notificationSchoolId =
    document.getElementById("notificationSchoolId");

const notificationMessage =
    document.getElementById("notificationMessage");

const sendNotificationBtn =
    document.getElementById("sendNotificationBtn");

const notificationsTable =
    document.getElementById("notificationsTable");


/* =====================================================
   LOAD NOTIFICATIONS
===================================================== */
async function loadNotifications() {

    try {

        const response = await fetch(
            "/api/notifications",
            {
                headers: {
                    "Authorization":
                        `Bearer ${localStorage.getItem("token")}`
                }
            }
        );

        const data = await response.json();

        notificationsTable.innerHTML = "";

        data.forEach(notification => {

            notificationsTable.innerHTML += `
                <tr>
                    <td>
                        ${notification.school_id}
                    </td>

                    <td>
                        ${notification.message}
                    </td>

                    <td>
                        ${notification.date}
                    </td>

                    <td>
                        <button
                            class="delete"
                            onclick="deleteNotification(${notification.id})"
                        >
                            Delete
                        </button>
                    </td>
                </tr>
            `;
        });

    } catch (error) {

        console.error(error);
    }
}


/* =====================================================
   SEND NOTIFICATION
===================================================== */
sendNotificationBtn.addEventListener(
    "click",
    async () => {

        const school_id =
            notificationSchoolId.value.trim();

        const message =
            notificationMessage.value.trim();

        if (!school_id || !message) {

            alert("All fields are required");
            return;
        }

        try {

            const response = await fetch(
                "/api/notifications",
                {
                    method: "POST",

                    headers: {
                        "Content-Type": "application/json",

                        "Authorization":
                            `Bearer ${localStorage.getItem("token")}`
                    },

                    body: JSON.stringify({
                        school_id,
                        message
                    })
                }
            );

            const data = await response.json();

            if (!response.ok) {
                alert(data.message);
                return;
            }

            alert(data.message);

            notificationSchoolId.value = "";
            notificationMessage.value = "";

            loadNotifications();

        } catch (error) {

            console.error(error);

            alert("Server error");
        }
    }
);


/* =====================================================
   DELETE NOTIFICATION
===================================================== */
async function deleteNotification(id) {

    if (!confirm("Delete notification?")) {
        return;
    }

    try {

        const response = await fetch(
            `/api/notifications/${id}`,
            {
                method: "DELETE",

                headers: {
                    "Authorization":
                        `Bearer ${localStorage.getItem("token")}`
                }
            }
        );

        const data = await response.json();

        alert(data.message);

        loadNotifications();

    } catch (error) {

        console.error(error);

        alert("Server error");
    }
}


/* =====================================================
   INITIAL LOAD
===================================================== */
loadNotifications();